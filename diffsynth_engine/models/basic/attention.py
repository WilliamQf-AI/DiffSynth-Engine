import torch
import torch.nn as nn
from einops import rearrange, repeat
from typing import Optional

import torch.nn.functional as F
from diffsynth_engine.utils import logging
from diffsynth_engine.utils.flag import (
    FLASH_ATTN_3_AVAILABLE,
    FLASH_ATTN_2_AVAILABLE,
    XFORMERS_AVAILABLE,
    SDPA_AVAILABLE,
    SAGE_ATTN_AVAILABLE,
    SPARGE_ATTN_AVAILABLE,
)

logger = logging.get_logger(__name__)


def memory_align(x: torch.Tensor, dim=-1, alignment: int = 8):
    padding_size = (alignment - x.shape[dim] % alignment) % alignment
    padded_x = F.pad(x, (0, padding_size), "constant", 0)
    return padded_x[..., : x.shape[dim]]


if FLASH_ATTN_3_AVAILABLE:
    from flash_attn_interface import flash_attn_func as flash_attn3
if FLASH_ATTN_2_AVAILABLE:
    from flash_attn import flash_attn_func as flash_attn2
if XFORMERS_AVAILABLE:
    from xformers.ops import memory_efficient_attention

    def xformers_attn(q, k, v, attn_mask=None, scale=None):
        if attn_mask is not None:
            attn_mask = repeat(attn_mask, "S L -> B H S L", B=q.shape[0], H=q.shape[2])
            attn_mask = memory_align(attn_mask)
        return memory_efficient_attention(q, k, v, attn_bias=attn_mask, scale=scale)


if SDPA_AVAILABLE:

    def sdpa_attn(q, k, v, attn_mask=None, scale=None):
        q = q.transpose(1, 2)
        k = k.transpose(1, 2)
        v = v.transpose(1, 2)
        out = torch.nn.functional.scaled_dot_product_attention(q, k, v, attn_mask=attn_mask, scale=scale)
        return out.transpose(1, 2)


if SAGE_ATTN_AVAILABLE:
    from sageattention import sageattn

    def sage_attn(q, k, v, attn_mask=None, scale=None):
        q = q.transpose(1, 2)
        k = k.transpose(1, 2)
        v = v.transpose(1, 2)
        out = sageattn(q, k, v, attn_mask=attn_mask, sm_scale=scale)
        return out.transpose(1, 2)


if SPARGE_ATTN_AVAILABLE:
    from spas_sage_attn import spas_sage2_attn_meansim_cuda

    def sparge_attn(self, q, k, v, attn_mask=None, scale=None):
        q = q.transpose(1, 2)
        k = k.transpose(1, 2)
        v = v.transpose(1, 2)
        out = spas_sage2_attn_meansim_cuda(q, k, v, attn_mask=attn_mask, scale=scale)
        return out.transpose(1, 2)


def eager_attn(q, k, v, attn_mask=None, scale=None):
    q = q.transpose(1, 2)
    k = k.transpose(1, 2)
    v = v.transpose(1, 2)
    scale = 1 / q.shape[-1] ** 0.5 if scale is None else scale
    q = q * scale
    attn = torch.matmul(q, k.transpose(-2, -1))
    if attn_mask is not None:
        attn = attn + attn_mask
    attn = attn.softmax(-1)
    out = attn @ v
    return out.transpose(1, 2)


def attention(
    q,
    k,
    v,
    attn_impl: Optional[str] = None,
    attn_mask: Optional[torch.Tensor] = None,
    scale: Optional[float] = None,
):
    """
    q: [B, Lq, Nq, C1]
    k: [B, Lk, Nk, C1]
    v: [B, Lk, Nk, C2]
    """
    assert attn_impl in [
        None,
        "auto",
        "eager",
        "flash_attn_2",
        "flash_attn_3",
        "xformers",
        "sdpa",
        "sage_attn",
        "sparge_attn",
    ]
    if attn_impl is None or attn_impl == "auto":
        if FLASH_ATTN_3_AVAILABLE:
            return flash_attn3(q, k, v, softmax_scale=scale)
        elif FLASH_ATTN_2_AVAILABLE:
            return flash_attn2(q, k, v, softmax_scale=scale)
        elif XFORMERS_AVAILABLE:
            return xformers_attn(q, k, v, attn_mask=attn_mask, scale=scale)
        elif SDPA_AVAILABLE:
            return sdpa_attn(q, k, v, attn_mask=attn_mask, scale=scale)
        else:
            return eager_attn(q, k, v, attn_mask=attn_mask, scale=scale)
    else:
        if attn_impl == "eager":
            return eager_attn(q, k, v, attn_mask=attn_mask, scale=scale)
        elif attn_impl == "flash_attn_3":
            return flash_attn3(q, k, v, softmax_scale=scale)
        elif attn_impl == "flash_attn_2":
            return flash_attn2(q, k, v, softmax_scale=scale)
        elif attn_impl == "xformers":
            return xformers_attn(q, k, v, attn_mask=attn_mask, scale=scale)
        elif attn_impl == "sdpa":
            return sdpa_attn(q, k, v, attn_mask=attn_mask, scale=scale)
        elif attn_impl == "sage_attn":
            return sage_attn(q, k, v, attn_mask=attn_mask, scale=scale)
        elif attn_impl == "sparge_attn":
            return sparge_attn(q, k, v, attn_mask=attn_mask, scale=scale)
        else:
            raise ValueError(f"Invalid attention implementation: {attn_impl}")


class Attention(nn.Module):
    def __init__(
        self,
        q_dim,
        num_heads,
        head_dim,
        kv_dim=None,
        bias_q=False,
        bias_kv=False,
        bias_out=False,
        scale=None,
        attn_impl: Optional[str] = None,
        device: str = "cuda:0",
        dtype: torch.dtype = torch.float16,
    ):
        super().__init__()
        dim_inner = head_dim * num_heads
        kv_dim = kv_dim if kv_dim is not None else q_dim
        self.num_heads = num_heads
        self.head_dim = head_dim

        self.to_q = nn.Linear(q_dim, dim_inner, bias=bias_q, device=device, dtype=dtype)
        self.to_k = nn.Linear(kv_dim, dim_inner, bias=bias_kv, device=device, dtype=dtype)
        self.to_v = nn.Linear(kv_dim, dim_inner, bias=bias_kv, device=device, dtype=dtype)
        self.to_out = nn.Linear(dim_inner, q_dim, bias=bias_out, device=device, dtype=dtype)
        self.attn_impl = attn_impl
        self.scale = scale

    def forward(
        self,
        x: torch.Tensor,
        y: Optional[torch.Tensor] = None,
        attn_mask: Optional[torch.Tensor] = None,
    ):
        if y is None:
            y = x
        q = rearrange(self.to_q(x), "b s (n d) -> b s n d", n=self.num_heads)
        k = rearrange(self.to_k(y), "b s (n d) -> b s n d", n=self.num_heads)
        v = rearrange(self.to_v(y), "b s (n d) -> b s n d", n=self.num_heads)
        out = attention(q, k, v, attn_mask=attn_mask, attn_impl=self.attn_impl, scale=self.scale)
        out = rearrange(out, "b s n d -> b s (n d)", n=self.num_heads)
        return self.to_out(out)


def long_context_attention(
    q,
    k,
    v,
    attn_impl: Optional[str] = None,
    attn_mask: Optional[torch.Tensor] = None,
    scale: Optional[float] = None,
):
    """
    q: [B, Lq, Nq, C1]
    k: [B, Lk, Nk, C1]
    v: [B, Lk, Nk, C2]
    """
    from yunchang import LongContextAttention
    from yunchang.kernels import AttnType

    assert attn_impl in [
        None,
        "auto",
        "eager",
        "flash_attn_2",
        "flash_attn_3",
        "xformers",
        "sdpa",
        "sage_attn",
        "sparge_attn",
    ]
    if attn_impl is None or attn_impl == "auto":
        if FLASH_ATTN_3_AVAILABLE:
            attn_func = LongContextAttention(attn_type=AttnType.FA3)
        elif FLASH_ATTN_2_AVAILABLE:
            attn_func = LongContextAttention(attn_type=AttnType.FA)
        elif SDPA_AVAILABLE:
            attn_func = LongContextAttention(attn_type=AttnType.TORCH)
        else:
            raise ValueError("No available long context attention implementation")
    else:
        if attn_impl == "flash_attn_3":
            attn_func = LongContextAttention(attn_type=AttnType.FA3)
        elif attn_impl == "flash_attn_2":
            attn_func = LongContextAttention(attn_type=AttnType.FA)
        elif attn_impl == "sdpa":
            attn_func = LongContextAttention(attn_type=AttnType.TORCH)
        elif attn_impl == "sage_attn":
            attn_func = LongContextAttention(attn_type=AttnType.SAGE_FP8)
        elif attn_impl == "sparge_attn":
            attn_func = LongContextAttention(attn_type=AttnType.SPARSE_SAGE)
        else:
            raise ValueError(f"Invalid long context attention implementation: {attn_impl}")
    return attn_func(q, k, v, softmax_scale=scale)
