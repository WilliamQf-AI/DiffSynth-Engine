import json
import torch
import torch.nn as nn
from typing import Dict

from diffsynth_engine.models.basic.timestep import TimestepEmbeddings
from diffsynth_engine.models.basic.unet_helper import (
    ResnetBlock,
    AttentionBlock,
    PushBlock,
    DownSampler,
    PopBlock,
    UpSampler,
)
from diffsynth_engine.models.base import PreTrainedModel, StateDictConverter, split_suffix
from diffsynth_engine.models.utils import no_init_weights
from diffsynth_engine.utils.constants import SDXL_UNET_CONFIG_FILE
from diffsynth_engine.utils import logging

logger = logging.get_logger(__name__)

with open(SDXL_UNET_CONFIG_FILE, "r") as f:
    config = json.load(f)


class SDXLUNetStateDictConverter(StateDictConverter):
    def _from_diffusers(self, state_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        # architecture
        block_types = [
            "ResnetBlock",
            "PushBlock",
            "ResnetBlock",
            "PushBlock",
            "DownSampler",
            "PushBlock",
            "ResnetBlock",
            "AttentionBlock",
            "PushBlock",
            "ResnetBlock",
            "AttentionBlock",
            "PushBlock",
            "DownSampler",
            "PushBlock",
            "ResnetBlock",
            "AttentionBlock",
            "PushBlock",
            "ResnetBlock",
            "AttentionBlock",
            "PushBlock",
            "ResnetBlock",
            "AttentionBlock",
            "ResnetBlock",
            "PopBlock",
            "ResnetBlock",
            "AttentionBlock",
            "PopBlock",
            "ResnetBlock",
            "AttentionBlock",
            "PopBlock",
            "ResnetBlock",
            "AttentionBlock",
            "UpSampler",
            "PopBlock",
            "ResnetBlock",
            "AttentionBlock",
            "PopBlock",
            "ResnetBlock",
            "AttentionBlock",
            "PopBlock",
            "ResnetBlock",
            "AttentionBlock",
            "UpSampler",
            "PopBlock",
            "ResnetBlock",
            "PopBlock",
            "ResnetBlock",
            "PopBlock",
            "ResnetBlock",
        ]

        # rename each parameter
        name_list = sorted([name for name in state_dict])
        rename_dict = {}
        block_id = {"ResnetBlock": -1, "AttentionBlock": -1, "DownSampler": -1, "UpSampler": -1}
        last_block_type_with_id = {"ResnetBlock": "", "AttentionBlock": "", "DownSampler": "", "UpSampler": ""}
        for name in name_list:
            names = name.split(".")
            if names[0] in ["conv_in", "conv_norm_out", "conv_out"]:
                pass
            elif names[0] in ["encoder_hid_proj"]:
                names[0] = "text_intermediate_proj"
            elif names[0] in ["time_embedding", "add_embedding"]:
                if names[0] == "add_embedding":
                    names[0] = "add_time_embedding"
                else:
                    names[0] = "time_embedding.timestep_embedder"
                names[1] = {"linear_1": "0", "linear_2": "2"}[names[1]]
            elif names[0] in ["down_blocks", "mid_block", "up_blocks"]:
                if names[0] == "mid_block":
                    names.insert(1, "0")
                block_type = {
                    "resnets": "ResnetBlock",
                    "attentions": "AttentionBlock",
                    "downsamplers": "DownSampler",
                    "upsamplers": "UpSampler",
                }[names[2]]
                block_type_with_id = ".".join(names[:4])
                if block_type_with_id != last_block_type_with_id[block_type]:
                    block_id[block_type] += 1
                last_block_type_with_id[block_type] = block_type_with_id
                while block_id[block_type] < len(block_types) and block_types[block_id[block_type]] != block_type:
                    block_id[block_type] += 1
                block_type_with_id = ".".join(names[:4])
                names = ["blocks", str(block_id[block_type])] + names[4:]
                if "ff" in names:
                    ff_index = names.index("ff")
                    component = ".".join(names[ff_index : ff_index + 3])
                    component = {"ff.net.0": "act_fn", "ff.net.2": "ff"}[component]
                    names = names[:ff_index] + [component] + names[ff_index + 3 :]
                if "to_out" in names:
                    names.pop(names.index("to_out") + 1)
            else:
                raise ValueError(f"Unknown parameters: {name}")
            rename_dict[name] = ".".join(names)

        # convert state_dict
        state_dict_ = {}
        for name, param in state_dict.items():
            if ".proj_in." in name or ".proj_out." in name:
                param = param.squeeze()
            state_dict_[rename_dict[name]] = param
        if "text_intermediate_proj.weight" in state_dict_:
            return state_dict_, {"is_kolors": True}
        else:
            return state_dict_

    def _from_civitai(self, state_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        rename_dict = config["civitai"]["rename_dict"]
        state_dict_ = {}
        for name, param in state_dict.items():
            name, suffix = split_suffix(name)
            if name in rename_dict:
                if ".proj_in." in name or ".proj_out." in name:
                    param = param.squeeze()
                name_ = rename_dict[name] + suffix
                state_dict_[name_] = param
        return state_dict_

    def convert(self, state_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        if "model.diffusion_model.input_blocks.0.0.weight" in state_dict:
            state_dict = self._from_civitai(state_dict)
            logger.info("use civitai format state dict")
        elif "down_blocks.0.resnets.0.conv1.weight" in state_dict:
            state_dict = self._from_diffusers(state_dict)
            logger.info("use diffusers format state dict")
        else:
            logger.info("user diffsynth format state dict")
        return state_dict


class SDXLUNet(PreTrainedModel):
    converter = SDXLUNetStateDictConverter()

    def __init__(
        self,
        is_kolors: bool = False,
        device: str = "cuda:0",
        dtype: torch.dtype = torch.float16,
        use_gradient_checkpointing: bool = False,
    ):
        super().__init__()
        self.use_gradient_checkpointing = use_gradient_checkpointing
        self.time_embedding = TimestepEmbeddings(dim_in=320, dim_out=1280, device=device, dtype=dtype)
        self.add_time_embedding = nn.Sequential(
            nn.Linear(5632 if is_kolors else 2816, 1280, device=device, dtype=dtype),
            nn.SiLU(),
            nn.Linear(1280, 1280, device=device, dtype=dtype),
        )
        self.conv_in = nn.Conv2d(4, 320, kernel_size=3, padding=1, device=device, dtype=dtype)
        self.text_intermediate_proj = nn.Linear(4096, 2048, device=device, dtype=dtype) if is_kolors else None

        self.blocks = nn.ModuleList(
            [
                # DownBlock2D
                ResnetBlock(320, 320, 1280, device=device, dtype=dtype),
                PushBlock(),
                ResnetBlock(320, 320, 1280, device=device, dtype=dtype),
                PushBlock(),
                DownSampler(320, device=device, dtype=dtype),
                PushBlock(),
                # CrossAttnDownBlock2D
                ResnetBlock(320, 640, 1280, device=device, dtype=dtype),
                AttentionBlock(10, 64, 640, 2, 2048, device=device, dtype=dtype),
                PushBlock(),
                ResnetBlock(640, 640, 1280, device=device, dtype=dtype),
                AttentionBlock(10, 64, 640, 2, 2048, device=device, dtype=dtype),
                PushBlock(),
                DownSampler(640, device=device, dtype=dtype),
                PushBlock(),
                # CrossAttnDownBlock2D
                ResnetBlock(640, 1280, 1280, device=device, dtype=dtype),
                AttentionBlock(20, 64, 1280, 10, 2048, device=device, dtype=dtype),
                PushBlock(),
                ResnetBlock(1280, 1280, 1280, device=device, dtype=dtype),
                AttentionBlock(20, 64, 1280, 10, 2048, device=device, dtype=dtype),
                PushBlock(),
                # UNetMidBlock2DCrossAttn
                ResnetBlock(1280, 1280, 1280, device=device, dtype=dtype),
                AttentionBlock(20, 64, 1280, 10, 2048, device=device, dtype=dtype),
                ResnetBlock(1280, 1280, 1280, device=device, dtype=dtype),
                # CrossAttnUpBlock2D
                PopBlock(),
                ResnetBlock(2560, 1280, 1280, device=device, dtype=dtype),
                AttentionBlock(20, 64, 1280, 10, 2048, device=device, dtype=dtype),
                PopBlock(),
                ResnetBlock(2560, 1280, 1280, device=device, dtype=dtype),
                AttentionBlock(20, 64, 1280, 10, 2048, device=device, dtype=dtype),
                PopBlock(),
                ResnetBlock(1920, 1280, 1280, device=device, dtype=dtype),
                AttentionBlock(20, 64, 1280, 10, 2048, device=device, dtype=dtype),
                UpSampler(1280, device=device, dtype=dtype),
                # CrossAttnUpBlock2D
                PopBlock(),
                ResnetBlock(1920, 640, 1280, device=device, dtype=dtype),
                AttentionBlock(10, 64, 640, 2, 2048, device=device, dtype=dtype),
                PopBlock(),
                ResnetBlock(1280, 640, 1280, device=device, dtype=dtype),
                AttentionBlock(10, 64, 640, 2, 2048, device=device, dtype=dtype),
                PopBlock(),
                ResnetBlock(960, 640, 1280, device=device, dtype=dtype),
                AttentionBlock(10, 64, 640, 2, 2048, device=device, dtype=dtype),
                UpSampler(640, device=device, dtype=dtype),
                # UpBlock2D
                PopBlock(),
                ResnetBlock(960, 320, 1280, device=device, dtype=dtype),
                PopBlock(),
                ResnetBlock(640, 320, 1280, device=device, dtype=dtype),
                PopBlock(),
                ResnetBlock(640, 320, 1280, device=device, dtype=dtype),
            ]
        )

        self.conv_norm_out = nn.GroupNorm(num_channels=320, num_groups=32, eps=1e-5, device=device, dtype=dtype)
        self.conv_act = nn.SiLU()
        self.conv_out = nn.Conv2d(320, 4, kernel_size=3, padding=1, device=device, dtype=dtype)

        self.is_kolors = is_kolors

    def forward(self, x, timestep, context, y, **kwargs):
        # 1. time embedding
        t_emb = self.time_embedding(timestep, dtype=x.dtype)
        ## add embedding
        add_embeds = self.add_time_embedding(y)

        time_emb = t_emb + add_embeds

        # 2. pre-process
        hidden_states = self.conv_in(x)
        text_emb = context if self.text_intermediate_proj is None else self.text_intermediate_proj(context)
        res_stack = [hidden_states]

        # 3. blocks
        for i, block in enumerate(self.blocks):
            if (
                self.training
                and self.use_gradient_checkpointing
                and not (isinstance(block, PushBlock) or isinstance(block, PopBlock))
            ):
                hidden_states, time_emb, text_emb, res_stack = torch.utils.checkpoint.checkpoint(
                    block,
                    hidden_states,
                    time_emb,
                    text_emb,
                    res_stack,
                    use_reentrant=False,
                )
            else:
                hidden_states, time_emb, text_emb, res_stack = block(
                    hidden_states,
                    time_emb,
                    text_emb,
                    res_stack,
                )

        # 4. output
        hidden_states = self.conv_norm_out(hidden_states)
        hidden_states = self.conv_act(hidden_states)
        hidden_states = self.conv_out(hidden_states)

        return hidden_states

    @classmethod
    def from_state_dict(
        cls, state_dict: Dict[str, torch.Tensor], device: str, dtype: torch.dtype, is_kolors: bool = False
    ):
        with no_init_weights():
            model = torch.nn.utils.skip_init(cls, device=device, dtype=dtype, is_kolors=is_kolors)
        model.load_state_dict(state_dict, assign=True)
        model.to(device=device, dtype=dtype, non_blocking=True)
        return model
