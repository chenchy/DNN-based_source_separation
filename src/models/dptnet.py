import math
import torch
import torch.nn as nn
import torch.nn.functional as F

from utils.utils_tasnet import choose_bases, choose_layer_norm
from models.glu import GLU
from models.dprnn_tasnet import Segment1d, OverlapAdd1d

EPS=1e-12

class DPTNet(nn.Module):
    def __init__(self, n_bases, kernel_size, stride=None, enc_bases=None, dec_bases=None, sep_hidden_channels=256, sep_chunk_size=100, sep_hop_size=50, sep_num_blocks=6, dilated=True, separable=True, causal=True, sep_norm=True, eps=EPS, mask_nonlinear='sigmoid', n_sources=2, **kwargs):
        super().__init__()
        
        if stride is None:
            stride = kernel_size//2
        
        assert kernel_size%stride == 0, "kernel_size is expected divisible by stride"
        
        # Encoder-decoder
        self.n_bases = n_bases
        self.kernel_size, self.stride = kernel_size, stride
        self.enc_bases, self.dec_bases = enc_bases, dec_bases
        
        if enc_bases == 'trainable' and not dec_bases == 'pinv':    
            self.enc_nonlinear = kwargs['enc_nonlinear']
        else:
            self.enc_nonlinear = None
        
        if enc_bases in ['Fourier', 'trainableFourier'] or dec_bases in ['Fourier', 'trainableFourier']:
            self.window_fn = kwargs['window_fn']
        else:
            self.window_fn = None
        
        # Separator configuration
        self.sep_hidden_channels = sep_hidden_channels
        self.sep_chunk_size, self.sep_hop_size = sep_chunk_size, sep_hop_size
        self.sep_num_blocks = sep_num_blocks
        
        self.dilated, self.separable, self.causal = dilated, separable, causal
        self.sep_norm = sep_norm
        self.mask_nonlinear = mask_nonlinear
        
        self.n_sources = n_sources
        self.eps = eps
        
        # Network configuration
        encoder, decoder = choose_bases(n_bases, kernel_size=kernel_size, stride=stride, enc_bases=enc_bases, dec_bases=dec_bases, **kwargs)
        
        self.encoder = encoder
        self.separator = Separator(n_bases, hidden_channels=sep_hidden_channels, chunk_size=sep_chunk_size, hop_size=sep_hop_size, num_blocks=sep_num_blocks, causal=causal, norm=sep_norm, mask_nonlinear=mask_nonlinear, n_sources=n_sources, eps=eps)
        self.decoder = decoder
        
        self.num_parameters = self._get_num_parameters()
        
    def forward(self, input):
        output, latent = self.extract_latent(input)
        
        return output
        
    def extract_latent(self, input):
        """
        Args:
            input (batch_size, 1, T)
        Returns:
            output (batch_size, n_sources, T)
            latent (batch_size, n_sources, n_bases, T'), where T' = (T-K)//S+1
        """
        n_sources = self.n_sources
        n_bases = self.n_bases
        kernel_size, stride = self.kernel_size, self.stride
        
        batch_size, C_in, T = input.size()
        
        assert C_in == 1, "input.size() is expected (?,1,?), but given {}".format(input.size())
        
        padding = (stride - (T-kernel_size)%stride)%stride
        padding_left = padding//2
        padding_right = padding - padding_left

        input = F.pad(input, (padding_left, padding_right))
        w = self.encoder(input)
        mask = self.separator(w)
        w = w.unsqueeze(dim=1)
        w_hat = w * mask
        latent = w_hat
        w_hat = w_hat.view(batch_size*n_sources, n_bases, -1)
        x_hat = self.decoder(w_hat)
        x_hat = x_hat.view(batch_size, n_sources, -1)
        output = F.pad(x_hat, (-padding_left, -padding_right))
        
        return output, latent
    
    def _get_num_parameters(self):
        num_parameters = 0
        
        for p in self.parameters():
            if p.requires_grad:
                num_parameters += p.numel()
                
        return num_parameters

class Separator(nn.Module):
    def __init__(self, num_features, hidden_channels=128, chunk_size=100, hop_size=50, num_blocks=6, causal=True, norm=True, out_nonlinear='tanh', mask_nonlinear='sigmoid', n_sources=2, eps=EPS):
        super().__init__()
        
        self.num_features, self.n_sources = num_features, n_sources
        self.chunk_size, self.hop_size = chunk_size, hop_size
        
        self.segment1d = Segment1d(chunk_size, hop_size)
        self.dprnn = DualPathTransformer(num_features, hidden_channels, num_blocks=num_blocks, causal=causal)
        self.overlap_add1d = OverlapAdd1d(chunk_size, hop_size)
        self.glu = GLU(num_features, n_sources*num_features, out_nonlinear=out_nonlinear)
        
        if mask_nonlinear == 'relu':
            self.mask_nonlinear = nn.ReLU()
        elif mask_nonlinear == 'sigmoid':
            self.mask_nonlinear = nn.Sigmoid()
        elif mask_nonlinear == 'softmax':
            self.mask_nonlinear = nn.Softmax(dim=1)
        else:
            raise ValueError("Cannot support {}".format(mask_nonlinear))
            
    def forward(self, input):
        """
        Args:
            input (batch_size, num_features, T_bin)
        Returns:
            output (batch_size, n_sources, num_features, T_bin)
        """
        num_features, n_sources = self.num_features, self.n_sources
        chunk_size, hop_size = self.chunk_size, self.hop_size
        batch_size, num_features, T_bin = input.size()
        
        padding = (hop_size-(T_bin-chunk_size)%hop_size)%hop_size
        padding_left = padding//2
        padding_right = padding - padding_left
        
        x = F.pad(input, (padding_left, padding_right))
        x = self.segment1d(x)
        x = self.dprnn(x)
        x = self.overlap_add1d(x)
        x = F.pad(x, (-padding_left, -padding_right))
        # x = self.prelu(x)
        x = self.glu(x)
        x = self.mask_nonlinear(x)
        output = x.view(batch_size, n_sources, num_features, T_bin)
        
        return output


class DualPathTransformer(nn.Module):
    def __init__(self, num_features, hidden_channels, num_blocks=6, causal=False, eps=EPS):
        super().__init__()
        
        # Network confguration
        net = []
        
        for _ in range(num_blocks):
            net.append(DualPathTransformerBlock(num_features, hidden_channels, causal=causal, eps=eps))
            
        self.net = nn.Sequential(*net)

    def forward(self, input):
        """
        Args:
            input (batch_size, num_features, S, chunk_size)
        Returns:
            output (batch_size, num_features, S, chunk_size)
        """
        output = self.net(input)

        return output

class DualPathTransformerBlock(nn.Module):
    def __init__(self, num_features, hidden_channels, causal, eps=EPS):
        super().__init__()
        
        self.intra_chunk_block = IntraChunkTransformer(num_features, hidden_channels, eps=eps)
        self.inter_chunk_block = InterChunkTransformer(num_features, hidden_channels, causal=causal, eps=eps)
        
    def forward(self, input):
        """
        Args:
            input (batch_size, num_features, S, chunk_size)
        Returns:
            output (batch_size, num_features, S, chunk_size)
        """
        x = self.intra_chunk_block(input)
        output = self.inter_chunk_block(x)
        
        return output

class IntraChunkTransformer(nn.Module):
    def __init__(self, num_features, hidden_channels, eps=EPS):
        super().__init__()
        
        raise NotImplementedError

class InterChunkTransformer(nn.Module):
    def __init__(self, num_features, hidden_channels, causal, eps=EPS):
        super().__init__()
        
        raise NotImplementedError