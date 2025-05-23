import json
import torch
from typing import Dict

from diffsynth_engine.models.components.vae import VAEDecoder, VAEEncoder, VAEStateDictConverter
from diffsynth_engine.models.utils import no_init_weights
from diffsynth_engine.utils.constants import FLUX_VAE_CONFIG_FILE
from diffsynth_engine.utils import logging

logger = logging.get_logger(__name__)

with open(FLUX_VAE_CONFIG_FILE, "r") as f:
    config = json.load(f)


class FluxVAEStateDictConverter(VAEStateDictConverter):
    def _from_civitai(self, state_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        rename_dict = config["civitai"]["rename_dict"]
        new_state_dict = {}
        for name, param in state_dict.items():
            if name not in rename_dict:
                continue
            name_ = rename_dict[name]
            if "transformer_blocks" in name_:
                param = param.squeeze()
            new_state_dict[name_] = param
        return new_state_dict

    def convert(self, state_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        assert self.has_decoder or self.has_encoder, "Either decoder or encoder must be present"
        if "decoder.conv_in.weight" in state_dict or "encoder.conv_in.weight" in state_dict:
            state_dict = self._from_civitai(state_dict)
            logger.info("use civitai format state dict")
        else:
            logger.info("use diffsynth format state dict")
        return self._filter(state_dict)


class FluxVAEEncoder(VAEEncoder):
    converter = FluxVAEStateDictConverter(has_encoder=True)

    def __init__(self, device: str = "cuda:0", dtype: torch.dtype = torch.float32):
        super().__init__(
            latent_channels=16,
            scaling_factor=0.3611,
            shift_factor=0.1159,
            use_quant_conv=False,
            device=device,
            dtype=dtype,
        )

    @classmethod
    def from_state_dict(cls, state_dict: Dict[str, torch.Tensor], device: str, dtype: torch.dtype):
        with no_init_weights():
            model = torch.nn.utils.skip_init(cls, device=device, dtype=dtype)
        model.load_state_dict(state_dict)
        return model


class FluxVAEDecoder(VAEDecoder):
    converter = FluxVAEStateDictConverter(has_decoder=True)

    def __init__(self, device: str, dtype: torch.dtype = torch.float32):
        super().__init__(
            latent_channels=16,
            scaling_factor=0.3611,
            shift_factor=0.1159,
            use_post_quant_conv=False,
            device=device,
            dtype=dtype,
        )

    @classmethod
    def from_state_dict(cls, state_dict: Dict[str, torch.Tensor], device: str, dtype: torch.dtype):
        with no_init_weights():
            model = torch.nn.utils.skip_init(cls, device=device, dtype=dtype)
        model.load_state_dict(state_dict)
        return model
