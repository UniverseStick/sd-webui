import os
import einops
from omegaconf import OmegaConf
import torch
import torch as th
import torch.nn as nn
from modules import devices, lowvram, shared

from ldm.modules.diffusionmodules.util import (
    conv_nd,
    linear,
    zero_module,
    timestep_embedding,
)

from ldm.modules.attention import SpatialTransformer
from ldm.modules.diffusionmodules.openaimodel import UNetModel, TimestepEmbedSequential, ResBlock, Downsample, AttentionBlock
from ldm.util import exists


def load_state_dict(ckpt_path, location='cpu'):
    _, extension = os.path.splitext(ckpt_path)
    if extension.lower() == ".safetensors":
        import safetensors.torch
        state_dict = safetensors.torch.load_file(ckpt_path, device=location)
    else:
        state_dict = get_state_dict(torch.load(
            ckpt_path, map_location=torch.device(location)))
    state_dict = get_state_dict(state_dict)
    print(f'Loaded state_dict from [{ckpt_path}]')
    return state_dict


def get_state_dict(d):
    return d.get('state_dict', d)


def align(hint, size):
    b, c, h1, w1 = hint.shape
    h, w = size
    if h != h1 or w != w1:
         hint = torch.nn.functional.interpolate(hint, size=size, mode="nearest")
    return hint


def get_node_name(name, parent_name):
    if len(name) <= len(parent_name):
        return False, ''
    p = name[:len(parent_name)]
    if p != parent_name:
        return False, ''
    return True, name[len(parent_name):]


class PlugableControlModel(nn.Module):
    def __init__(self, model_path, config_path, weight=1.0, lowvram=False, base_model=None) -> None:
        super().__init__()
        config = OmegaConf.load(config_path)
        
        self.control_model = ControlNet(**config.model.params.control_stage_config.params)
        state_dict = load_state_dict(model_path)
        
        if any([k.startswith("control_model.") for k, v in state_dict.items()]):
            
            is_diff_model = 'difference' in state_dict
            transfer_ctrl_opt = shared.opts.data.get("control_net_control_transfer", False) and \
                any([k.startswith("model.diffusion_model.") for k, v in state_dict.items()])
                
            if (is_diff_model or transfer_ctrl_opt) and base_model is not None:
                # apply transfer control - https://github.com/lllyasviel/ControlNet/blob/main/tool_transfer_control.py
                
                unet_state_dict = base_model.state_dict()
                unet_state_dict_keys = unet_state_dict.keys()
                final_state_dict = {}
                counter = 0
                for key in state_dict.keys():
                    if not key.startswith("control_model."):
                        continue
                    
                    p = state_dict[key]
                    is_control, node_name = get_node_name(key, 'control_')
                    key_name = node_name.replace("model.", "") if is_control else key

                    if key_name in unet_state_dict_keys:
                        if is_diff_model:
                            # transfer control by make difference in advance
                            p_new = p + unet_state_dict[key_name].clone().cpu()
                        else:
                            # transfer control by calculate offsets from (delta = p + current_unet_encoder - frozen_unet_encoder)
                            p_new = p + unet_state_dict[key_name].clone().cpu() - state_dict["model.diffusion_model."+key_name]
                        counter += 1
                    else:
                        p_new = p
                    final_state_dict[key] = p_new
                    
                print(f'Offset cloned: {counter} values')
                state_dict = final_state_dict
                
            state_dict = {k.replace("control_model.", ""): v for k, v in state_dict.items() if k.startswith("control_model.")}
        else:
            # assume that model is done by user
            pass
            
        self.control_model.load_state_dict(state_dict)
        self.lowvram = lowvram            
        self.weight = weight
        self.only_mid_control = False
        self.control = None
        self.hint_cond = None
        
        if not self.lowvram:
            self.control_model.to(devices.get_device_for("controlnet"))

    def hook(self, model, parent_model):
        outer = self

        def forward(self, x, timesteps=None, context=None, **kwargs):
            only_mid_control = outer.only_mid_control
            
            # hires stuffs
            # note that this method may not works if hr_scale < 1.1
            if abs(x.shape[-1] - outer.hint_cond.shape[-1] // 8) > 8:
                only_mid_control = shared.opts.data.get("control_net_only_midctrl_hires", True)
                # If you want to completely disable control net, uncomment this.
                # return self._original_forward(x, timesteps=timesteps, context=context, **kwargs)
            
            control = outer.control_model(x=x, hint=outer.hint_cond, timesteps=timesteps, context=context)
            assert timesteps is not None, ValueError(f"insufficient timestep: {timesteps}")
            hs = []
            with torch.no_grad():
                t_emb = timestep_embedding(
                    timesteps, self.model_channels, repeat_only=False)
                emb = self.time_embed(t_emb)
                h = x.type(self.dtype)
                for module in self.input_blocks:
                    h = module(h, emb, context)
                    hs.append(h)
                h = self.middle_block(h, emb, context)

            h += control.pop()

            for i, module in enumerate(self.output_blocks):
                if only_mid_control:
                    h = torch.cat([h, hs.pop()], dim=1)
                else:
                    hs_input, control_input = hs.pop(), control.pop()
                    h = align(h, hs_input.shape[-2:])
                    h = torch.cat([h, hs_input + control_input * outer.weight], dim=1)
                h = module(h, emb, context)

            h = h.type(x.dtype)
            return self.out(h)

        def forward2(*args, **kwargs):
            # webui will handle other compoments 
            try:
                if shared.cmd_opts.lowvram:
                    lowvram.send_everything_to_cpu()
                if self.lowvram:
                    self.control_model.to(devices.get_device_for("controlnet"))
                return forward(*args, **kwargs)
            finally:
                if self.lowvram:
                    self.control_model.cpu()
        
        model._original_forward = model.forward
        model.forward = forward2.__get__(model, UNetModel)
    
    def notify(self, cond_like, weight):
        self.hint_cond = cond_like
        self.weight = weight
        # print(self.hint_cond.shape)

    def restore(self, model):
        if not hasattr(model, "_original_forward"):
            # no such handle, ignore
            return
        
        model.forward = model._original_forward
        del model._original_forward


class ControlNet(nn.Module):
    def __init__(
        self,
        image_size,
        in_channels,
        model_channels,
        hint_channels,
        num_res_blocks,
        attention_resolutions,
        dropout=0,
        channel_mult=(1, 2, 4, 8),
        conv_resample=True,
        dims=2,
        use_checkpoint=False,
        use_fp16=False,
        num_heads=-1,
        num_head_channels=-1,
        num_heads_upsample=-1,
        use_scale_shift_norm=False,
        resblock_updown=False,
        use_new_attention_order=False,
        use_spatial_transformer=False,    # custom transformer support
        transformer_depth=1,              # custom transformer support
        context_dim=None,                 # custom transformer support
        # custom support for prediction of discrete ids into codebook of first stage vq model
        n_embed=None,
        legacy=True,
        disable_self_attentions=None,
        num_attention_blocks=None,
        disable_middle_self_attn=False,
        use_linear_in_transformer=False,
    ):
        super().__init__()
        if use_spatial_transformer:
            assert context_dim is not None, 'Fool!! You forgot to include the dimension of your cross-attention conditioning...'

        if context_dim is not None:
            assert use_spatial_transformer, 'Fool!! You forgot to use the spatial transformer for your cross-attention conditioning...'
            from omegaconf.listconfig import ListConfig
            if type(context_dim) == ListConfig:
                context_dim = list(context_dim)

        if num_heads_upsample == -1:
            num_heads_upsample = num_heads

        if num_heads == -1:
            assert num_head_channels != -1, 'Either num_heads or num_head_channels has to be set'

        if num_head_channels == -1:
            assert num_heads != -1, 'Either num_heads or num_head_channels has to be set'

        self.dims = dims
        self.image_size = image_size
        self.in_channels = in_channels
        self.model_channels = model_channels
        if isinstance(num_res_blocks, int):
            self.num_res_blocks = len(channel_mult) * [num_res_blocks]
        else:
            if len(num_res_blocks) != len(channel_mult):
                raise ValueError("provide num_res_blocks either as an int (globally constant) or "
                                 "as a list/tuple (per-level) with the same length as channel_mult")
            self.num_res_blocks = num_res_blocks
        if disable_self_attentions is not None:
            # should be a list of booleans, indicating whether to disable self-attention in TransformerBlocks or not
            assert len(disable_self_attentions) == len(channel_mult)
        if num_attention_blocks is not None:
            assert len(num_attention_blocks) == len(self.num_res_blocks)
            assert all(map(lambda i: self.num_res_blocks[i] >= num_attention_blocks[i], range(
                len(num_attention_blocks))))
            print(f"Constructor of UNetModel received num_attention_blocks={num_attention_blocks}. "
                  f"This option has LESS priority than attention_resolutions {attention_resolutions}, "
                  f"i.e., in cases where num_attention_blocks[i] > 0 but 2**i not in attention_resolutions, "
                  f"attention will still not be set.")

        self.attention_resolutions = attention_resolutions
        self.dropout = dropout
        self.channel_mult = channel_mult
        self.conv_resample = conv_resample
        self.use_checkpoint = use_checkpoint
        self.dtype = th.float16 if use_fp16 else th.float32
        self.num_heads = num_heads
        self.num_head_channels = num_head_channels
        self.num_heads_upsample = num_heads_upsample
        self.predict_codebook_ids = n_embed is not None

        time_embed_dim = model_channels * 4
        self.time_embed = nn.Sequential(
            linear(model_channels, time_embed_dim),
            nn.SiLU(),
            linear(time_embed_dim, time_embed_dim),
        )

        self.input_blocks = nn.ModuleList(
            [
                TimestepEmbedSequential(
                    conv_nd(dims, in_channels, model_channels, 3, padding=1)
                )
            ]
        )
        self.zero_convs = nn.ModuleList([self.make_zero_conv(model_channels)])

        self.input_hint_block = TimestepEmbedSequential(
            conv_nd(dims, hint_channels, 16, 3, padding=1),
            nn.SiLU(),
            conv_nd(dims, 16, 16, 3, padding=1),
            nn.SiLU(),
            conv_nd(dims, 16, 32, 3, padding=1, stride=2),
            nn.SiLU(),
            conv_nd(dims, 32, 32, 3, padding=1),
            nn.SiLU(),
            conv_nd(dims, 32, 96, 3, padding=1, stride=2),
            nn.SiLU(),
            conv_nd(dims, 96, 96, 3, padding=1),
            nn.SiLU(),
            conv_nd(dims, 96, 256, 3, padding=1, stride=2),
            nn.SiLU(),
            zero_module(conv_nd(dims, 256, model_channels, 3, padding=1))
        )

        self._feature_size = model_channels
        input_block_chans = [model_channels]
        ch = model_channels
        ds = 1
        for level, mult in enumerate(channel_mult):
            for nr in range(self.num_res_blocks[level]):
                layers = [
                    ResBlock(
                        ch,
                        time_embed_dim,
                        dropout,
                        out_channels=mult * model_channels,
                        dims=dims,
                        use_checkpoint=use_checkpoint,
                        use_scale_shift_norm=use_scale_shift_norm,
                    )
                ]
                ch = mult * model_channels
                if ds in attention_resolutions:
                    if num_head_channels == -1:
                        dim_head = ch // num_heads
                    else:
                        num_heads = ch // num_head_channels
                        dim_head = num_head_channels
                    if legacy:
                        #num_heads = 1
                        dim_head = ch // num_heads if use_spatial_transformer else num_head_channels
                    if exists(disable_self_attentions):
                        disabled_sa = disable_self_attentions[level]
                    else:
                        disabled_sa = False

                    if not exists(num_attention_blocks) or nr < num_attention_blocks[level]:
                        layers.append(
                            AttentionBlock(
                                ch,
                                use_checkpoint=use_checkpoint,
                                num_heads=num_heads,
                                num_head_channels=dim_head,
                                use_new_attention_order=use_new_attention_order,
                            ) if not use_spatial_transformer else SpatialTransformer(
                                ch, num_heads, dim_head, depth=transformer_depth, context_dim=context_dim,
                                disable_self_attn=disabled_sa, use_linear=use_linear_in_transformer,
                                use_checkpoint=use_checkpoint
                            )
                        )
                self.input_blocks.append(TimestepEmbedSequential(*layers))
                self.zero_convs.append(self.make_zero_conv(ch))
                self._feature_size += ch
                input_block_chans.append(ch)
            if level != len(channel_mult) - 1:
                out_ch = ch
                self.input_blocks.append(
                    TimestepEmbedSequential(
                        ResBlock(
                            ch,
                            time_embed_dim,
                            dropout,
                            out_channels=out_ch,
                            dims=dims,
                            use_checkpoint=use_checkpoint,
                            use_scale_shift_norm=use_scale_shift_norm,
                            down=True,
                        )
                        if resblock_updown
                        else Downsample(
                            ch, conv_resample, dims=dims, out_channels=out_ch
                        )
                    )
                )
                ch = out_ch
                input_block_chans.append(ch)
                self.zero_convs.append(self.make_zero_conv(ch))
                ds *= 2
                self._feature_size += ch

        if num_head_channels == -1:
            dim_head = ch // num_heads
        else:
            num_heads = ch // num_head_channels
            dim_head = num_head_channels
        if legacy:
            #num_heads = 1
            dim_head = ch // num_heads if use_spatial_transformer else num_head_channels
        self.middle_block = TimestepEmbedSequential(
            ResBlock(
                ch,
                time_embed_dim,
                dropout,
                dims=dims,
                use_checkpoint=use_checkpoint,
                use_scale_shift_norm=use_scale_shift_norm,
            ),
            AttentionBlock(
                ch,
                use_checkpoint=use_checkpoint,
                num_heads=num_heads,
                num_head_channels=dim_head,
                use_new_attention_order=use_new_attention_order,
                # always uses a self-attn
            ) if not use_spatial_transformer else SpatialTransformer(
                ch, num_heads, dim_head, depth=transformer_depth, context_dim=context_dim,
                disable_self_attn=disable_middle_self_attn, use_linear=use_linear_in_transformer,
                use_checkpoint=use_checkpoint
            ),
            ResBlock(
                ch,
                time_embed_dim,
                dropout,
                dims=dims,
                use_checkpoint=use_checkpoint,
                use_scale_shift_norm=use_scale_shift_norm,
            ),
        )
        self.middle_block_out = self.make_zero_conv(ch)
        self._feature_size += ch

    def make_zero_conv(self, channels):
        return TimestepEmbedSequential(zero_module(conv_nd(self.dims, channels, channels, 1, padding=0)))
    
    def align(self, hint, h, w):
        c, h1, w1 = hint.shape
        if h != h1 or w != w1:
            hint = align(hint.unsqueeze(0), (h, w))
            return hint.squeeze(0)
        return hint

    def forward(self, x, hint, timesteps, context, **kwargs):
        t_emb = timestep_embedding(
            timesteps, self.model_channels, repeat_only=False)
        emb = self.time_embed(t_emb)

            
        guided_hint = self.input_hint_block(hint, emb, context)
        outs = []
        
        h1, w1 = x.shape[-2:]
        guided_hint = self.align(guided_hint, h1, w1)

        h = x.type(self.dtype)
        for module, zero_conv in zip(self.input_blocks, self.zero_convs):
            if guided_hint is not None:
                h = module(h, emb, context)
                h += guided_hint
                guided_hint = None
            else:
                h = module(h, emb, context)
            outs.append(zero_conv(h, emb, context))

        h = self.middle_block(h, emb, context)
        outs.append(self.middle_block_out(h, emb, context))

        return outs