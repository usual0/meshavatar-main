# Copyright (c) 2020-2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved. 
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction, 
# disclosure or distribution of this material and related documentation 
# without an express license agreement from NVIDIA CORPORATION or 
# its affiliates is strictly prohibited.

import torch
import nvdiffrast.torch as dr

from . import util
from . import renderutils as ru
from . import optixutils as ou
from . import light

rnd_seed = 0

# ==============================================================================================
#  Helper functions
# ==============================================================================================
def interpolate(attr, rast, attr_idx, rast_db=None):
    return dr.interpolate(attr.contiguous(), rast, attr_idx, rast_db=rast_db, diff_attrs=None if rast_db is None else 'all')

# ==============================================================================================
#  pixel shader
# ==============================================================================================

def shade(
        gb_pos,
        gb_geometric_normal,
        gb_normal,
        gb_tangent,
        gb_texc,
        gb_texc_deriv,
        msk,
        view_pos,
        lgt,
        material,
        bsdf,
        cano_pos,
        cond,
        optix_ctx,
        gb_depth,
        denoiser,
        shadow_scale
    ):

    offset = torch.normal(mean=0, std=0.005, size=(gb_depth.shape[0], gb_depth.shape[1], gb_depth.shape[2], 2), device="cuda")
    jitter = (util.pixel_grid(gb_depth.shape[2], gb_depth.shape[1])[None, ...] + offset).contiguous()

    mask = msk.float()[..., None]
    mask_tap = dr.texture(mask.contiguous(), jitter, filter_mode='linear', boundary_mode='clamp')
    grad_weight = mask * mask_tap

    ################################################################################
    # Texture lookups
    ################################################################################
    perturbed_nrm = None
    if 'kd_ks_normal' in material:
        # Combined texture, used for MLPs because lookups are expensive
        if cano_pos is None:
            cano_pos = gb_pos
        all_tex_jitter = material['kd_ks_normal'].sample(cano_pos + torch.normal(mean=0, std=0.01, size=cano_pos.shape, device=gb_pos.device), cond, msk)
        all_tex = material['kd_ks_normal'].sample(cano_pos, cond, msk)
        assert all_tex.shape[-1] == 9 or all_tex.shape[-1] == 10, "Combined kd_ks_normal must be 9 or 10 channels"
        kd, ks, perturbed_nrm = all_tex[..., :-6], all_tex[..., -6:-3], all_tex[..., -3:]
        # Compute albedo (kd) gradient, used for material regularizer
        kd_grad    = torch.abs(all_tex_jitter[..., :-6] - all_tex[..., :-6])
        ks_grad    = torch.abs(all_tex_jitter[..., -6:-3] - all_tex[..., -6:-3]) * torch.tensor([0, 1, 1], dtype=torch.float32, device=gb_pos.device)
    elif 'radiance' in material:
        if cano_pos is None:
            cano_pos = gb_pos
        view_dir = util.safe_normalize(view_pos - gb_pos)
        all_tex_jitter = material['radiance'].sample(cano_pos + torch.normal(mean=0, std=0.01, size=cano_pos.shape, device=gb_pos.device), view_dir, cond, msk)
        all_tex = material['radiance'].sample(cano_pos, view_dir, cond, msk)
        kd = all_tex
        ks = torch.zeros_like(kd)
        kd_grad = torch.abs(all_tex_jitter - all_tex)
        kd_grad = torch.zeros_like(kd_grad)
        ks_grad = torch.zeros_like(kd_grad)
    else:
        kd_jitter  = material['kd'].sample(gb_texc + torch.normal(mean=0, std=0.005, size=gb_texc.shape, device=gb_pos.device), gb_texc_deriv)
        ks_jitter  = material['ks'].sample(gb_texc + torch.normal(mean=0, std=0.005, size=gb_texc.shape, device=gb_pos.device), gb_texc_deriv)[..., 0:3]
        kd = material['kd'].sample(gb_texc, gb_texc_deriv)
        ks = material['ks'].sample(gb_texc, gb_texc_deriv)[..., 0:3] # skip alpha
        if 'normal' in material:
            perturbed_nrm = material['normal'].sample(gb_texc, gb_texc_deriv)
        kd_grad    = torch.abs(kd_jitter[..., 0:3] - kd[..., 0:3])
        ks_grad    = torch.abs(ks_jitter[..., 0:3] - ks[..., 0:3]) * torch.tensor([0, 1, 1], dtype=torch.float32, device=gb_pos.device)

    # Separate kd into alpha and color, default alpha = 1
    alpha = kd[..., 3:4] if kd.shape[-1] == 4 else torch.ones_like(kd[..., 0:1]) 
    kd = kd[..., 0:3]

    ################################################################################
    # Normal perturbation & normal bend
    ################################################################################
    if 'no_perturbed_nrm' in material and material['no_perturbed_nrm']:
        perturbed_nrm = None

    # Geometric smoothed normal regularizer
    nrm_jitter = dr.texture(gb_normal.contiguous(), jitter, filter_mode='linear', boundary_mode='clamp')
    nrm_grad = torch.abs(nrm_jitter - gb_normal) * grad_weight

    if perturbed_nrm is not None:
        perturbed_nrm_jitter = dr.texture(perturbed_nrm.contiguous(), jitter, filter_mode='linear', boundary_mode='clamp')
        perturbed_nrm_grad = 1.0 - util.safe_normalize(util.safe_normalize(perturbed_nrm_jitter) + util.safe_normalize(perturbed_nrm))[..., 2:3]
        perturbed_nrm_grad = perturbed_nrm_grad.repeat(1,1,1,3) * grad_weight

    gb_normal = ru.prepare_shading_normal(gb_pos, view_pos, perturbed_nrm, gb_normal, gb_tangent, gb_geometric_normal, two_sided_shading=True, opengl=True)

    ################################################################################
    # Evaluate BSDF
    ################################################################################

    assert 'bsdf' in material or bsdf is not None, "Material must specify a BSDF type"
    bsdf = material['bsdf'] if bsdf is None else bsdf
    if bsdf == 'pbr-optix' or bsdf == 'diffuse-optix' or bsdf == 'white-optix':
        kd = torch.ones_like(kd) if bsdf == 'white-optix' else kd

        assert isinstance(lgt, light.EnvironmentLightOptix) and optix_ctx is not None
        ro = gb_pos + gb_normal*0.001

        global rnd_seed
        diffuse_accum, specular_accum = ou.optix_env_shade(optix_ctx, msk.float(), ro, gb_pos, gb_normal, view_pos, kd, ks, 
                            lgt.base, lgt._pdf, lgt.rows[:,0], lgt.cols, BSDF=bsdf, n_samples_x=8 if denoiser is not None else 64, 
                            rnd_seed=rnd_seed, shadow_scale=shadow_scale)
        rnd_seed += 1

        # denoise demodulated shaded values if possible
        if denoiser is not None:
            diffuse_accum  = denoiser.forward(torch.cat((diffuse_accum, gb_normal, gb_depth), dim=-1))
            specular_accum = denoiser.forward(torch.cat((specular_accum, gb_normal, gb_depth), dim=-1))

        if bsdf == 'white-optix' or bsdf == 'diffuse-optix':
            shaded_col = diffuse_accum * kd
        else:
            kd = kd * (1.0 - ks[..., 2:3]) # kd * (1.0 - metalness)
            shaded_col = diffuse_accum * kd + specular_accum

        # denoise combined shaded values if possible
        if denoiser is not None:
            shaded_col = denoiser.forward(torch.cat((shaded_col, gb_normal, gb_depth), dim=-1))
    elif bsdf == 'pbr':
        if isinstance(lgt, light.EnvironmentLight):
            shaded_col = lgt.shade(gb_pos, gb_normal, kd, ks, view_pos, specular=True)
        else:
            assert False, "Invalid light type"
    elif bsdf == 'diffuse':
        if isinstance(lgt, light.EnvironmentLight):
            shaded_col = lgt.shade(gb_pos, gb_normal, kd, ks, view_pos, specular=False)
        else:
            assert False, "Invalid light type"
    elif bsdf == 'radiance':
        shaded_col = kd
    elif bsdf == 'normal':
        shaded_col = (gb_normal + 1.0)*0.5
    elif bsdf == 'tangent':
        shaded_col = (gb_tangent + 1.0)*0.5
    elif bsdf == 'corr':
        shaded_col = (cano_pos + torch.tensor([1.0, 1.4, 1.0], device=cano_pos.device)) * 0.5
    elif bsdf == 'kd':
        shaded_col = kd
    elif bsdf == 'ks':
        shaded_col = ks
    else:
        assert False, "Invalid BSDF '%s'" % bsdf
    
    # Return multiple buffers
    buffers = {
        'shaded'    : torch.cat((shaded_col, alpha), dim=-1),
        'kd_grad'   : torch.cat((kd_grad, alpha), dim=-1),
        'ks_grad'   : torch.cat((ks_grad, alpha), dim=-1),
        'normal_grad'   : torch.cat((nrm_grad, alpha), dim=-1),
        # 'ks'        : torch.cat((ks, alpha), dim=-1), # for debug
        'occlusion' : torch.cat((ks[..., :1], alpha), dim=-1),
        'gb_pos': torch.cat((gb_pos, alpha), dim=-1),
        'gb_normal': torch.cat((gb_normal, alpha), dim=-1)
    }

    if 'diffuse_accum' in locals():
        buffers['diffuse_light'] = torch.cat((diffuse_accum, alpha), dim=-1)
    if 'specular_accum' in locals():
        buffers['specular_light'] = torch.cat((specular_accum, alpha), dim=-1)
    return buffers

# ==============================================================================================
#  Render a depth slice of the mesh (scene), some limitations:
#  - Single mesh
#  - Single light
#  - Single material
# ==============================================================================================
def render_layer(
        v_pos_clip,
        rast,
        rast_deriv,
        mesh,
        view_pos,
        lgt,
        resolution,
        spp,
        msaa,
        bsdf,
        cano_pos,
        cond,
        optix_ctx,
        denoiser,
        shadow_scale
    ):

    full_res = [resolution[0]*spp, resolution[1]*spp]

    ################################################################################
    # Rasterize
    ################################################################################

    # Scale down to shading resolution when MSAA is enabled, otherwise shade at full resolution
    if spp > 1 and msaa:
        rast_out_s = util.scale_img_nhwc(rast, resolution, mag='nearest', min='nearest')
        rast_out_deriv_s = util.scale_img_nhwc(rast_deriv, resolution, mag='nearest', min='nearest') * spp
    else:
        rast_out_s = rast
        rast_out_deriv_s = rast_deriv

    # import ipdb; ipdb.set_trace()
    ################################################################################
    # Interpolate attributes
    ################################################################################

    # Interpolate world space position
    gb_pos, _ = interpolate(mesh.v_pos, rast_out_s, mesh.t_pos_idx[0].int())
    msk = rast_out_s[..., -1] > 0
    if cano_pos is not None:
        cano_pos, _ = interpolate(cano_pos, rast_out_s, mesh.t_pos_idx[0].int())

    # Compute geometric normals. We need those because of bent normals trick (for bump mapping)
    v0 = mesh.v_pos[:, mesh.t_pos_idx[0, :, 0], :]
    v1 = mesh.v_pos[:, mesh.t_pos_idx[0, :, 1], :]
    v2 = mesh.v_pos[:, mesh.t_pos_idx[0, :, 2], :]
    face_normals = util.safe_normalize(torch.cross(v1 - v0, v2 - v0, dim=-1))
    num_faces = face_normals.shape[1]
    face_normal_indices = (torch.arange(0, num_faces, dtype=torch.int64, device='cuda')[:, None]).repeat(1, 3)
    gb_geometric_normal, _ = interpolate(face_normals, rast_out_s, face_normal_indices.int())

    # Compute tangent space
    assert mesh.v_nrm is not None and mesh.v_tng is not None
    gb_normal, _ = interpolate(mesh.v_nrm, rast_out_s, mesh.t_nrm_idx[0].int())
    gb_tangent, _ = interpolate(mesh.v_tng, rast_out_s, mesh.t_tng_idx[0].int()) # Interpolate tangents

    # Texture coordinate
    assert mesh.v_tex is not None
    gb_texc, gb_texc_deriv = interpolate(mesh.v_tex, rast_out_s, mesh.t_tex_idx[0].int(), rast_db=rast_out_deriv_s)

    # Interpolate z and z-gradient
    with torch.no_grad():
        eps = 0.00001
        clip_pos, clip_pos_deriv = interpolate(v_pos_clip, rast_out_s, mesh.t_pos_idx[0].int(), rast_db=rast_out_deriv_s)
        z0 = torch.clamp(clip_pos[..., 2:3], min=eps) / torch.clamp(clip_pos[..., 3:4], min=eps)
        z1 = torch.clamp(clip_pos[..., 2:3] + torch.abs(clip_pos_deriv[..., 2:3]), min=eps) / torch.clamp(clip_pos[..., 3:4] + torch.abs(clip_pos_deriv[..., 3:4]), min=eps)
        z_grad = torch.abs(z1 - z0)
        gb_depth = torch.cat((z0, z_grad), dim=-1)

    ################################################################################
    # Shade
    ################################################################################

    buffers = shade(gb_pos, gb_geometric_normal, gb_normal, gb_tangent, gb_texc, gb_texc_deriv, msk,
        view_pos, lgt, mesh.material, bsdf, cano_pos, cond, optix_ctx, gb_depth, denoiser, shadow_scale)

    ################################################################################
    # Prepare output
    ################################################################################

    # Scale back up to visibility resolution if using MSAA
    if spp > 1 and msaa:
        for key in buffers.keys():
            buffers[key] = util.scale_img_nhwc(buffers[key], full_res, mag='nearest', min='nearest')

    # Return buffers
    return buffers

# ==============================================================================================
#  Render a depth peeled mesh (scene), some limitations:
#  - Single light
#  - Single material
# ==============================================================================================
def render_mesh(
        ctx,
        mesh,
        mtx_in,
        view_pos,
        lgt,
        resolution,
        spp         = 1,
        num_layers  = 1,
        msaa        = False,
        background  = None, 
        bsdf        = None,
        cano_pos    = None,
        cond        = None,
        optix_ctx   = None,
        denoiser    = None,
        shadow_scale = 1.0
    ):

    def prepare_input_vector(x):
        x = torch.tensor(x, dtype=torch.float32, device='cuda') if not torch.is_tensor(x) else x
        return x[:, None, None, :] if len(x.shape) == 2 else x
    
    def composite_buffer(key, layers, background, antialias):
        accum = background
        for buffers, rast in reversed(layers):
            alpha = (rast[..., -1:] > 0).float() * buffers[key][..., -1:]
            accum = torch.lerp(accum, torch.cat((buffers[key][..., :-1], torch.ones_like(buffers[key][..., -1:])), dim=-1), alpha)
            if antialias:
                accum = dr.antialias(accum.contiguous(), rast, v_pos_clip, mesh.t_pos_idx[0].int())
        return accum
    # print(background.shape)
    # print(resolution)
    assert mesh.t_pos_idx.shape[0] > 0, "Got empty training triangle mesh (unrecoverable discontinuity)"
    assert background is None or (background.shape[1] == resolution[0] and background.shape[2] == resolution[1])

    full_res = [resolution[0]*spp, resolution[1]*spp]

    # Convert numpy arrays to torch tensors
    mtx_in      = torch.tensor(mtx_in, dtype=torch.float32, device='cuda') if not torch.is_tensor(mtx_in) else mtx_in
    view_pos    = prepare_input_vector(view_pos)

    # clip space transform
    v_pos_clip = ru.xfm_points(mesh.v_pos, mtx_in, use_python=False)

    # Render all layers front-to-back
    layers = []
    with dr.DepthPeeler(ctx, v_pos_clip, mesh.t_pos_idx[0].int(), full_res) as peeler:
        for _ in range(num_layers):
            rast, db = peeler.rasterize_next_layer()
            layers += [(render_layer(v_pos_clip, rast, db, mesh, view_pos, lgt, resolution, spp, msaa, bsdf, cano_pos, cond, optix_ctx, denoiser, shadow_scale), rast)]

    # Setup background
    if background is not None:
        if spp > 1:
            background = util.scale_img_nhwc(background, full_res, mag='nearest', min='nearest')
        background = torch.cat((background, torch.zeros_like(background[..., 0:1])), dim=-1)
    else:
        background = torch.zeros(1, full_res[0], full_res[1], 4, dtype=torch.float32, device='cuda')

    # Composite layers front-to-back
    out_buffers = {}
    for key in layers[0][0].keys():
        if key == 'shaded':
            accum = composite_buffer(key, layers, background, True)
        else:
            accum = composite_buffer(key, layers, torch.zeros_like(layers[0][0][key]), False)

        # Downscale to framebuffer resolution. Use avg pooling 
        out_buffers[key] = util.avg_pool_nhwc(accum, spp) if spp > 1 else accum

    return out_buffers

# ==============================================================================================
#  Render UVs
# ==============================================================================================
def render_uv(ctx, mesh, resolution, mlp_texture, cond=None, cano_pos=None):

    if cano_pos is None:
        cano_pos = mesh.v_pos

    # clip space transform 
    uv_clip = mesh.v_tex * 2.0 - 1.0

    # pad to four component coordinate
    uv_clip4 = torch.cat((uv_clip, torch.zeros_like(uv_clip[...,0:1]), torch.ones_like(uv_clip[...,0:1])), dim = -1)

    # rasterize
    rast, _ = dr.rasterize(ctx, uv_clip4, mesh.t_tex_idx[0].int(), resolution)

    # Interpolate world space position
    gb_pos, _ = interpolate(cano_pos, rast, mesh.t_pos_idx[0].int())

    # Sample out textures from MLP
    all_tex = mlp_texture.sample(gb_pos, cond)
    assert all_tex.shape[-1] == 9 or all_tex.shape[-1] == 10, "Combined kd_ks_normal must be 9 or 10 channels"
    perturbed_nrm = all_tex[..., -3:]
    return (rast[..., -1:] > 0).float(), all_tex[..., :-6], all_tex[..., -6:-3], util.safe_normalize(perturbed_nrm)
