import math
import sys

from re import U
import torch
import torch.nn as nn
import torch.nn.functional as F
from functools import partial

from einops import rearrange, repeat

try:
    from src.ops.fftconv import fftconv_ref, fftconv_func

except ImportError:
    fftconv_func = None

try:
    from flash_attn.ops.fused_dense import FusedDense
except ImportError:
    FusedDense = None

import src.utils.registry as registry
from src.utils.train import OptimModule
from src.utils.config import instantiate, auto_assign_attrs
from src.models.nn import Activation


class FFTConvFuncv2(torch.autograd.Function):
    @staticmethod
    def forward(ctx, u, k):
        seqlen = u.shape[-1]
        if len(u.shape) > 3:
            k = k.unsqueeze(1)
        fft_size = 2 * seqlen

        k_f = torch.fft.rfft(k, n=fft_size) / fft_size
        u_f = torch.fft.rfft(u.to(dtype=k.dtype), n=fft_size)
        y = torch.fft.irfft(u_f * k_f, n=fft_size, norm="forward")[..., :seqlen]
        ctx.save_for_backward(u_f, k_f)
        return y

    @staticmethod
    def backward(ctx, dout):
        u_f, k_f = ctx.saved_tensors
        seqlen = dout.shape[-1]
        fft_size = 2 * seqlen

        dout_f = torch.fft.rfft(dout, n=fft_size)
        du = torch.fft.irfft(dout_f * k_f.conj(), n=fft_size, norm="forward")[
            ..., :seqlen
        ]
        dk = torch.fft.irfft(dout_f * u_f.conj(), n=fft_size, norm="forward")[
            ..., :seqlen
        ]
        return du, dk.squeeze()


def fftconv_ref(u, k, D, dropout_mask, gelu=True, k_rev=None, bidirectional=False):
    seqlen = u.shape[-1]
    fft_size = 2 * seqlen
    k_f = torch.fft.rfft(k, n=fft_size) / fft_size
    if k_rev is not None:
        k_rev_f = torch.fft.rfft(k_rev, n=fft_size) / fft_size
        k_f = k_f + k_rev_f.conj()

    if bidirectional:
        # we need to pad 1/2 before and 1/2 after the input
        padded_length = seqlen + 2 * (seqlen // 2)
        pad_before = padded_length // 2 - (seqlen // 2)
        pad_after = padded_length - seqlen - pad_before
        padded_u = F.pad(u, (pad_before, pad_after), mode="constant", value=0)
        u_f = torch.fft.rfft(padded_u.to(dtype=k.dtype), n=fft_size)
    else:
        u_f = torch.fft.rfft(u.to(dtype=k.dtype), n=fft_size)

    if len(u.shape) > 3:
        k_f = k_f.unsqueeze(1)

    y = torch.fft.irfft(u_f * k_f, n=fft_size, norm="forward")[..., :seqlen]

    out = y + u * D.unsqueeze(-1)
    if gelu:
        out = F.gelu(out)
    if dropout_mask is not None:
        return (out * rearrange(dropout_mask, "b H -> b H 1")).to(dtype=u.dtype)
    else:
        return out.to(dtype=u.dtype)


@torch.jit.script
def mul_sum(q, y):
    return (q * y).sum(dim=1)


class Sin(nn.Module):
    def __init__(self, dim, w=10, train_freq=True):
        super().__init__()
        self.freq = (
            nn.Parameter(w * torch.ones(1, dim))
            if train_freq
            else w * torch.ones(1, dim)
        )

    def forward(self, x):
        return torch.sin(self.freq * x)


class PositionalEmbedding(OptimModule):
    def __init__(self, emb_dim: int, seq_len: int, lr_pos_emb: float = 1e-5, **kwargs):
        """Complex exponential positional embeddings for Hyena filters."""
        super().__init__()

        self.seq_len = seq_len
        # The time embedding fed to the filteres is normalized so that t_f = 1
        t = torch.linspace(0, 1, self.seq_len)[None, :, None]  # 1, L, 1

        if emb_dim > 1:
            bands = (emb_dim - 1) // 2
        # To compute the right embeddings we use the "proper" linspace
        t_rescaled = torch.linspace(0, seq_len - 1, seq_len)[None, :, None]
        w = 2 * math.pi * t_rescaled / seq_len  # 1, L, 1

        f = torch.linspace(1e-4, bands - 1, bands)[None, None]
        z = torch.exp(-1j * f * w)
        z = torch.cat([t, z.real, z.imag], dim=-1)
        self.register("z", z, lr=lr_pos_emb)
        self.register("t", t, lr=0.0)

    def forward(self, L):
        return self.z[:, :L], self.t[:, :L]


class ExponentialModulation(OptimModule):
    def __init__(
        self,
        d_model,
        fast_decay_pct=0.3,
        slow_decay_pct=1.5,
        target=1e-2,
        modulation_lr=0.0,
        shift: float = 0.0,
        **kwargs,
    ):
        super().__init__()
        self.shift = shift
        max_decay = math.log(target) / fast_decay_pct
        min_decay = math.log(target) / slow_decay_pct
        deltas = torch.linspace(min_decay, max_decay, d_model)[None, None]
        self.register("deltas", deltas, lr=modulation_lr)

    def forward(self, t, x):
        decay = torch.exp(-t * self.deltas.abs())
        x = x * (decay + self.shift)
        return x


class HyenaFilter(OptimModule):
    def __init__(
        self,
        d_model,
        emb_dim=3,  # dim of input to MLP, augments with positional encoding
        order=16,  # width of the implicit MLP
        fused_fft_conv=False,
        seq_len=1024,
        lr=1e-3,
        lr_pos_emb=1e-5,
        dropout=0.0,
        w=1,  # frequency of periodic activations
        wd=0,  # weight decay of kernel parameters
        bias=True,
        num_inner_mlps=2,
        linear_mixer=False,
        modulate: bool = True,
        normalized=False,
        bidirectional=False,
        **kwargs,
    ):
        """
        Implicit long filter with modulation.
        Args:
            d_model: number of channels in the input
            emb_dim: dimension of the positional encoding (`emb_dim` - 1) // 2 is the number of bands
            order: width of the FFN
            num_inner_mlps: number of inner linear layers inside filter MLP
        Note:
            filter_dropout is not implemented
        """
        super().__init__()
        auto_assign_attrs(
            self, d_model=d_model, emb_dim=emb_dim, seq_len=seq_len, modulate=modulate
        )
        self.use_bias = bias
        self.fused_fft_conv = fused_fft_conv
        self.bias = nn.Parameter(torch.randn(self.d_model))
        self.dropout = nn.Dropout(dropout)
        self.bidirectional = bidirectional

        act = Sin(dim=order, w=w)
        assert (
            emb_dim % 2 != 0 and emb_dim >= 3
        ), "emb_dim must be odd and greater or equal to 3 (time, sine and cosine)"
        self.pos_emb = PositionalEmbedding(emb_dim, seq_len, lr_pos_emb)

        # uses a variable number of inner linear layers
        if linear_mixer is False:
            self.implicit_filter = nn.Sequential(
                nn.Linear(emb_dim, order),
                act,
            )
            for i in range(num_inner_mlps):
                self.implicit_filter.append(nn.Linear(order, order))
                self.implicit_filter.append(act)
            # final linear layer
            self.implicit_filter.append(nn.Linear(order, d_model, bias=False))
        else:
            self.implicit_filter = nn.Sequential(
                nn.Linear(emb_dim, d_model, bias=False),
            )

        self.modulation = ExponentialModulation(d_model, **kwargs)

        self.normalized = normalized
        for c in self.implicit_filter.children():
            for name, v in c.state_dict().items():
                optim = {"weight_decay": wd, "lr": lr}
                setattr(getattr(c, name), "_optim", optim)

    def filter(self, L, *args, **kwargs):
        z, t = self.pos_emb(L)
        h = self.implicit_filter(z)
        if self.modulate:
            h = self.modulation(t, h)

        if self.normalized:
            h = h / torch.norm(h, dim=-1, p=1, keepdim=True)

        return h

    def forward(self, x, L, k=None, bias=None, *args, **kwargs):
        if k is None:
            k = self.filter(L)

        # Ensure compatibility with filters that return a tuple
        k = k[0] if type(k) is tuple else k
        if bias is None:
            bias = self.bias
        bias = bias if self.use_bias else 0 * bias

        if self.fused_fft_conv:
            bias = bias.to(dtype=torch.float32)
            y = fftconv_func(
                x,
                k,
                bias,
                dropout_mask=None,
                gelu=False,
                force_fp16_output=torch.is_autocast_enabled(),
            )
        else:
            y = fftconv_ref(
                x,
                k,
                bias,
                dropout_mask=None,
                gelu=False,
                bidirectional=self.bidirectional,
            )
            # y = (
            #     FFTConvFuncv2.apply(x, k.to(dtype=torch.float32))
            #     + bias.unsqueeze(-1) * x
            # )

        return y.to(dtype=x.dtype)


class HyenaOperator(nn.Module):
    def __init__(
        self,
        d_model,  #
        l_max,  #
        order=2,  #
        filter_order=64,
        num_heads=1,
        inner_factor=1,
        num_blocks=1,
        fused_bias_fc=False,
        outer_mixing=False,
        dropout=0.0,  #
        filter_dropout=0.0,  #
        filter_cls="hyena-filter",
        post_order_ffn=False,
        jit_filter=False,
        short_filter_order=3,
        activation="id",
        return_state=False,
        **filter_args,
    ):
        r"""
        Hyena operator described in the paper https://arxiv.org/pdf/2302.10866.pdf
        Args:
            d_model (int): Dimension of the input and output embeddings (width of the layer)
            l_max: (int): Maximum input sequence length. Defaults to None
            order: (int): Depth of the Hyena recurrence. Defaults to 2
            filter_order: (int): Width of the FFN parametrizing the implicit filter. Defaults to 64
            num_heads: (int): Number of heads. Defaults to 1
            inner_factor: (int): Width multiplier. Defaults to 1
            num_blocks: (int): Number of blocks in sequence length. Defaults to 1
            fused_bias_fc: (bool): Whether to use fused bias FC. Defaults to False
            dropout: (float): Dropout probability. Defaults to 0.0
            filter_dropout: (float): Dropout probability for the filter. Defaults to 0.0
            post_order_ffn: (bool): Apply a dense layer between steps of the recurrence. Defaults to False
            jit_filter: (bool): Whether JIT the implicit filter function. Defaults to False
            short_filter_order: (int): Length of the explicit input convolutional filter. Defaults to 3
            activation: (str): type of act between kernel output and FF (default identity)
            return_state: (bool): whether to return a state
        """
        super().__init__()
        assert (
            d_model % num_heads == 0
        ), f"Model dimension {d_model} must be divisible by num heads {num_heads}"
        assert (
            l_max % num_blocks == 0
        ), f"Maximum signal length {l_max} must be divisible by block dimension {num_blocks}"
        block_dim = l_max // num_blocks
        head_dim = d_model // num_heads

        auto_assign_attrs(
            self,
            d_model=d_model,
            order=order,
            l_max=l_max,
            num_heads=num_heads,
            inner_factor=inner_factor,
            block_dim=block_dim,
            head_dim=head_dim,
            filter_order=filter_order,
            post_order_ffn=post_order_ffn,
            short_filter_order=short_filter_order,
            num_blocks=num_blocks,
            filter_dropout=filter_dropout,
            jit_filter=jit_filter,
            outer_mixing=outer_mixing,
            activation=activation,
            return_state=return_state,
        )
        self.activation = Activation(activation)
        self.dropout = nn.Dropout(dropout)
        self.setup_projections(fused_bias_fc, inner_factor)
        self.setup_filters(filter_cls, filter_args)

    def setup_projections(self, fused_bias_fc, inner_factor):
        "Initializes input and output projections (over the width dimension)"
        if fused_bias_fc and FusedDense is None:
            raise ImportError("fused_dense is not installed")
        linear_cls = nn.Linear if not fused_bias_fc else FusedDense
        self.out_proj = linear_cls(self.d_model * inner_factor, self.d_model)
        self.in_proj = linear_cls(self.d_model, (self.order + 1) * self.d_model)
        if self.post_order_ffn:
            self.ord_proj_w = nn.Parameter(
                torch.randn(self.order, self.num_heads, self.num_heads)
                / math.sqrt(self.head_dim)
            )

    def setup_filters(self, filter_cls, filter_args):
        "Initializes the explicit and implicit filters"
        assert self.order >= 2, f"Order must be at least 2, (got {self.order})"
        total_width = self.d_model * self.inner_factor * (self.order + 1)

        self.short_filter = nn.Conv1d(
            in_channels=total_width,
            out_channels=total_width,
            kernel_size=self.short_filter_order,
            groups=total_width,
            padding=self.short_filter_order - 1,
        )

        filter_cls = instantiate(registry.layer, filter_cls, partial=True)

        self.filter_fn = filter_cls(
            self.head_dim * self.inner_factor * (self.order - 1),
            order=self.filter_order,
            seq_len=self.l_max,
            channels=1,
            dropout=self.filter_dropout,
            **filter_args,
        )
        if self.jit_filter:
            self.filter_fn = torch.jit.script(self.filter_fn, self.L)

    def recurrence(self, u, state):
        "Fast inference mode via distilled recurrence"
        raise NotImplementedError("Working on it!")

    def forward(self, u, *args, **kwargs):
        l = u.size(-2)
        l_filter = min(l, self.l_max)
        u = self.in_proj(u)
        u = rearrange(u, "b l d -> b d l")

        uc = self.short_filter(u)[..., :l_filter]

        uc = rearrange(
            uc,
            "b (ho v) (z l) -> b ho v z l",
            z=self.num_blocks,
            ho=self.num_heads,
            v=self.head_dim * (self.order + 1),
        )

        *x, v = uc.split(self.d_model, dim=2)
        k = self.filter_fn.filter(l_filter)

        # `c` is always 1 by default
        k = rearrange(k, "c l (v o) -> c o v l", v=self.head_dim, o=self.order - 1)[0]

        bias = rearrange(
            self.filter_fn.bias, "(v o) -> o v", v=self.head_dim, o=self.order - 1
        )

        for o, x_i in enumerate(reversed(x[1:])):
            if self.outer_mixing:
                v = rearrange(v, "b h v z l -> b h 1 v z l")
                v = self.dropout(v * rearrange(x_i, "b h v z l -> b h v 1 z l"))
                v = v.sum(dim=2)
            else:
                v = self.dropout(v * x_i)

            # the bias term is broadcasted. Last dimension (l) is handled by fftconv
            v = self.filter_fn(v, l_filter, k=k[o], bias=bias[o, None, :, None])

            if self.post_order_ffn:
                w = self.ord_proj_w[o]
                v = mul_sum(
                    rearrange(w, "h1 h2 -> 1 h1 h2 1 1 1"),
                    rearrange(v, "b h v z l -> b h 1 v z l"),
                )

        y = self.activation(
            rearrange(
                v * x[0],
                "b h v z l -> b (z l) (h v)",
                z=self.num_blocks,
                h=self.num_heads,
            )
        )
        y = self.out_proj(y)

        if self.return_state:
            return y, None
        return y

    @property
    def d_output(self):
        return self.d_model


def orchid_conv(u, k, bias, dilation=2):
    weight = k.unsqueeze(1)  # shape: (C, 1, kernel_size)
    pad = dilation * (k.shape[-1] - 1) // 2
    out = F.conv1d(
        u,
        weight,
        bias=bias,
        stride=1,
        padding=pad,
        dilation=dilation,
        groups=u.shape[1],
    )
    return out


class ConvKernelNN(nn.Module):
    def __init__(self, d_model, seq_len, hidden_dim=None):
        super().__init__()
        if hidden_dim is None:
            hidden_dim = d_model
        self.conv1 = nn.Conv1d(
            in_channels=d_model,
            out_channels=hidden_dim,
            kernel_size=3,
            padding=1,
            groups=d_model,
        )
        self.relu = nn.ReLU()
        self.conv2 = nn.Conv1d(
            in_channels=hidden_dim,
            out_channels=d_model,
            kernel_size=3,
            padding=1,
            groups=d_model,
        )
        self.seq_len = seq_len

    def forward(self, v):
        # v: (B, L, d_model)
        x = v.transpose(1, 2)
        x = self.conv1(x)
        x = self.relu(x)
        x = self.conv2(x)
        x_f = torch.fft.rfft(x, n=self.seq_len, dim=-1)
        x_abs = torch.abs(x_f)
        x_mean = x_abs.mean(dim=0, keepdim=True)
        x_ifft = torch.fft.irfft(x_mean, n=self.seq_len, dim=-1)
        return x_ifft  # (1, d_model, L)


class OrchidFilter(OptimModule):
    def __init__(
        self,
        d_model,
        emb_dim=3,
        order=16,
        seq_len=1024,
        kernel_size=3,
        dropout=0.0,
        w=1,
        lr_pos_emb=1e-5,
        num_inner_mlps=2,
        **kwargs,
    ):
        super().__init__()
        self.d_model = d_model
        self.kernel_size = kernel_size
        self.order = order
        self.dropout = nn.Dropout(dropout)
        self.act = nn.GELU()
        self.emb_dim = emb_dim
        assert emb_dim % 2 != 0 and emb_dim >= 3, "emb_dim must be odd and >= 3"
        self.seq_len = seq_len
        self.pos_emb = PositionalEmbedding(emb_dim, seq_len, lr_pos_emb=lr_pos_emb)
        self.implicit_filter = nn.Sequential(
            nn.Linear(emb_dim, d_model * kernel_size),
            self.act,
        )
        for i in range(num_inner_mlps):
            self.implicit_filter.append(
                nn.Linear(d_model * kernel_size, d_model * kernel_size)
            )
            self.implicit_filter.append(self.act)
        self.implicit_filter.append(nn.Linear(d_model * kernel_size, d_model))
        self.modulation = ExponentialModulation(d_model, **kwargs)
        self.register("bias", torch.randn((order - 1) * d_model))

    def filter(self, L):
        z, t = self.pos_emb(L)
        h = self.implicit_filter(z)  # (1, L, d_model)
        h = self.modulation(t, h)
        return h

    def forward(self, x, L, k=None, bias=None, dilation=2):
        if k is None:
            k = self.filter(L)
        k = k[0] if type(k) is tuple else k
        if bias is None:
            bias = self.bias.view(self.order - 1, self.d_model)
        v = x
        for o in range(self.order - 1):
            v = orchid_conv(v, k[o], bias[o], dilation=dilation)
        return v


class DiscreteTransform(nn.Module):

    def __init__(self, mode="dct", norm="ortho", dim=1):
        super().__init__()
        self.mode = mode
        self.norm = norm
        self.dim = dim

    def forward(self, x):
        if self.mode == "dct":
            return torch.fft.dct(x, type=2, norm=self.norm, dim=self.dim)
        elif self.mode == "fft":
            return torch.fft.rfft(x, dim=self.dim)
        else:
            raise ValueError("Unsupported mode")

    def inverse(self, x):
        if self.mode == "dct":
            return torch.fft.idct(x, type=2, norm=self.norm, dim=self.dim)
        elif self.mode == "fft":
            return torch.fft.irfft(x, dim=self.dim)
        else:
            raise ValueError("Unsupported mode")


class OrchidOperator(nn.Module):
    def __init__(
        self,
        d_model,
        l_max,
        order=2,
        dropout=0.0,
        filter_dropout=0.0,
        dilation=2,
        kernel_size=3,
        **filter_args,
    ):
        super().__init__()
        self.d_model = d_model
        self.l_max = l_max
        self.order = order
        self.dilation = dilation
        inner_width = d_model * (order + 1)
        self.dropout = nn.Dropout(dropout)
        self.in_proj = nn.Linear(d_model, inner_width)
        self.out_proj = nn.Linear(d_model, d_model)
        self.short_filter = nn.Conv1d(
            inner_width, inner_width, kernel_size=3, padding=2, groups=inner_width
        )
        self.filter_fn = OrchidFilter(
            d_model * (order - 1),
            emb_dim=3,
            order=order,
            seq_len=l_max,
            kernel_size=kernel_size,
            dropout=filter_dropout,
            **filter_args,
        )
        self.conv_kernel_nn = ConvKernelNN(d_model=d_model, seq_len=l_max)
        self.transform = DiscreteTransform(mode="fft", dim=-1)

    def forward(self, u, *args, **kwargs):
        l = u.size(-2)
        l_filter = min(l, self.l_max)
        u = self.in_proj(u)
        u = rearrange(u, "b l d -> b d l")
        uc = self.short_filter(u)[..., :l_filter]
        *x, v = uc.split(self.d_model, dim=1)

        h_adapt_f = self.conv_kernel_nn(v.transpose(1, 2))
        h_adapt_f = h_adapt_f[..., :l_filter]
        h_adapt_f = rearrange(h_adapt_f, "b c l -> b l c")
        h_adapt_f = self.transform(h_adapt_f)

        h_0 = self.filter_fn.filter(l_filter)
        h_0 = rearrange(h_0, "1 l d -> l d")
        h_0_f = self.transform(h_0.unsqueeze(0))

        y = v * x[0]  # (B, d_model, l_filter)
        y = rearrange(y, "b d l -> b l d")  # (B, l_filter, d_model)
        y_f = self.transform(y)  # (B, l_filter, d_model_fft)

        combined_kernel = h_0_f + h_adapt_f
        y_new = torch.fft.irfft(y_f * combined_kernel, dim=-1)
        y_new = y_new[..., :l_filter]  # (B, l_filter, d_model)

        if len(x) > 1:
            x1 = rearrange(x[1], "b d l -> b l d")
            y = y_new * x1
        else:
            y = y_new

        y = self.out_proj(y)
        return y
