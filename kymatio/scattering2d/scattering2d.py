# Authors: Edouard Oyallon
# Scientific Ancestry: Edouard Oyallon, Laurent Sifre, Joan Bruna


__all__ = ['Scattering2D']

import torch
from .backend import cdgmm, Modulus, SubsampleFourier, fft, Pad, unpad, convert_filters
from .filter_bank import filter_bank
from .utils import compute_padding


class Scattering2D(object):
    """Main module implementing the scattering transform in 2D.
    The scattering transform computes two wavelet transform followed
    by modulus non-linearity.
    It can be summarized as::

        S_J x = [S_J^0 x, S_J^1 x, S_J^2 x]

    where::

        S_J^0 x = x * phi_J
        S_J^1 x = [|x * psi^1_lambda| * phi_J]_lambda
        S_J^2 x = [||x * psi^1_lambda| * psi^2_mu| * phi_J]_{lambda, mu}

    where * denotes the convolution (in space), phi_J is a low pass
    filter, psi^1_lambda is a family of band pass
    filters and psi^2_mu is another family of band pass filters.
    Only Morlet filters are used in this implementation.
    Convolutions are efficiently performed in the Fourier domain
    with this implementation.

    Example
    -------
        # 1) Define a Scattering object as:
        s = Scattering2D(J, shape=(M, N))
        #    where (M, N) are the image sizes and 2**J the scale of the scattering
        # 2) Forward on an input Tensor x of shape B x M x N,
        #     where B is the batch size.
        result_s = s(x)

    Parameters
    ----------
    J : int
        logscale of the scattering
    shape : tuple of int
        spatial support (M, N) of the input
    L : int, optional
        number of angles used for the wavelet transform
    max_order : int, optional
        The maximum order of scattering coefficients to compute. Must be either
        `1` or `2`. Defaults to `2`.
    pre_pad : boolean, optional
        controls the padding: if set to False, a symmetric padding is applied
        on the signal. If set to true, the software will assume the signal was
        padded externally.

    Attributes
    ----------
    J : int
        logscale of the scattering
    shape : tuple of int
        spatial support (M, N) of the input
    L : int, optional
        number of angles used for the wavelet transform
    max_order : int, optional
        The maximum order of scattering coefficients to compute.
        Must be either equal to `1` or `2`. Defaults to `2`.
    pre_pad : boolean
        controls the padding
    Psi : dictionary
        containing the wavelets filters at all resolutions. See
        filter_bank.filter_bank for an exact description.
    Phi : dictionary
        containing the low-pass filters at all resolutions. See
        filter_bank.filter_bank for an exact description.
    M_padded, N_padded : int
         spatial support of the padded input

    Notes
    -----
    The design of the filters is optimized for the value L = 8

    pre_pad is particularly useful when doing crops of a bigger
     image because the padding is then extremely accurate. Defaults
     to False.

    """
    def __init__(self, J, shape, L=8, max_order=2, pre_pad=False):
        self.J, self.L = J, L
        self.pre_pad = pre_pad
        self.max_order = max_order
        self.shape = shape
        if 2**J>shape[0] or 2**J>shape[1]:
            raise (RuntimeError('The smallest dimension should be larger than 2^J'))

        self.build()

    def build(self):
        self.M, self.N = self.shape
        self.modulus = Modulus()
        self.M_padded, self.N_padded = compute_padding(self.M, self.N, self.J)
        # pads equally on a given side if the amount of padding to add is an even number of pixels, otherwise it adds an extra pixel
        self.pad = Pad([(self.M_padded - self.M) // 2, (self.M_padded - self.M+1) // 2, (self.N_padded - self.N) // 2, (self.N_padded - self.N + 1) // 2], [self.M, self.N], pre_pad=self.pre_pad)
        self.subsample_fourier = SubsampleFourier()
        # Create the filters
        filters = filter_bank(self.M_padded, self.N_padded, self.J, self.L)
        self.Psi = convert_filters(filters['psi'])
        self.Phi = convert_filters([filters['phi'][j] for j in range(self.J)])

    def _apply(self, fn):
        """
            Mimics the behavior of the function _apply() of a nn.Module()
        """
        for key, item in enumerate(self.Psi):
            for key2, item2 in self.Psi[key].items():
                if torch.is_tensor(item2):
                    self.Psi[key][key2] = fn(item2)
        self.Phi = [fn(v) for v in self.Phi]
        self.pad.padding_module._apply(fn)
        return self

    def cuda(self, device=None):
        """
            Mimics the behavior of the function cuda() of a nn.Module()
        """
        return self._apply(lambda t: t.cuda(device))

    def to(self, *args, **kwargs):
        """
            Mimics the behavior of the function to() of a nn.Module()
        """
        device, dtype, non_blocking = torch._C._nn._parse_to(*args, **kwargs)

        if dtype is not None:
            if not dtype.is_floating_point:
                raise TypeError('nn.Module.to only accepts floating point '
                                'dtypes, but got desired dtype={}'.format(dtype))

        def convert(t):
            return t.to(device, dtype if t.is_floating_point() else None, non_blocking)

        return self._apply(convert)

    def cpu(self):
        """
            Mimics the behavior of the function cpu() of a nn.Module()
        """
        return self._apply(lambda t: t.cpu())

    def forward(self, input):
        """Forward pass of the scattering.

        Parameters
        ----------
        input : tensor
            tensor with 3 dimensions :math:`(B, C, M, N)` where :math:`(B, C)` are arbitrary.
            :math:`B` typically is the batch size, whereas :math:`C` is the number of input channels.

        Returns
        -------
        S : tensor
            scattering of the input, a 4D tensor :math:`(B, C, D, Md, Nd)` where :math:`D` corresponds
            to a new channel dimension and :math:`(Md, Nd)` are downsampled sizes by a factor :math:`2^J`.

        """
        if not torch.is_tensor(input):
            raise(TypeError('The input should be a torch.cuda.FloatTensor, a torch.FloatTensor or a torch.DoubleTensor'))

        if len(input.shape) < 2:
            raise (RuntimeError('Input tensor must have at least two '
                'dimensions'))

        if (not input.is_contiguous()):
            raise (RuntimeError('Tensor must be contiguous!'))

        if((input.size(-1)!=self.N or input.size(-2)!=self.M) and not self.pre_pad):
            raise (RuntimeError('Tensor must be of spatial size (%i,%i)!'%(self.M,self.N)))

        if ((input.size(-1) != self.N_padded or input.size(-2) != self.M_padded) and self.pre_pad):
            raise (RuntimeError('Padded tensor must be of spatial size (%i,%i)!' % (self.M_padded, self.N_padded)))

        batch_shape = input.shape[:-2]
        signal_shape = input.shape[-2:]

        input = input.reshape((-1, 1) + signal_shape)

        J = self.J
        phi = self.Phi
        psi = self.Psi

        subsample_fourier = self.subsample_fourier
        modulus = self.modulus
        pad = self.pad
        order0_size = 1
        order1_size = self.L * J
        order2_size = self.L ** 2 * J * (J - 1) // 2

        ### YST addition ###
        order3_size = self.L ** 3 * (J * (J - 1) * (J-2)) // (3 * 2)
        order4_size = self.L ** 4 * (J * (J - 1) * (J-2) * (J-3)) // (4 * 3 * 2)

        output_size = order0_size + order1_size

        if self.max_order >= 2:
            output_size += order2_size

        ### YST addition ###
        if self.max_order >= 3:
            output_size += order3_size
        if self.max_order >= 4:
            output_size += order4_size
        #print('output size:', output_size)

        S = input.new(input.size(0),
                      input.size(1),
                      output_size,
                      self.M_padded//(2**J)-2,
                      self.N_padded//(2**J)-2)
        U_r = pad(input)
        U_0_c = fft(U_r, 'C2C')  # We trick here with U_r and U_2_c

        # First low pass filter
        U_1_c = subsample_fourier(cdgmm(U_0_c, phi[0]), k=2**J)

        U_J_r = fft(U_1_c, 'C2R')

        S[..., 0, :, :] = unpad(U_J_r)
        n_order1 = 1
        n_order2 = 1 + order1_size

        ### YST addition ###
        n_order3 = 1 + order1_size + order2_size
        n_order4 = 1 + order1_size + order2_size + order3_size

        for n1 in range(len(psi)):
            j1 = psi[n1]['j']
            U_1_c = cdgmm(U_0_c, psi[n1][0])
            if(j1 > 0):
                U_1_c = subsample_fourier(U_1_c, k=2 ** j1)
            U_1_c = fft(U_1_c, 'C2C', inverse=True)
            U_1_c = fft(modulus(U_1_c), 'C2C')

            # Second low pass filter
            U_2_c = subsample_fourier(cdgmm(U_1_c, phi[j1]), k=2**(J-j1))
            U_J_r = fft(U_2_c, 'C2R')
            S[..., n_order1, :, :] = unpad(U_J_r)
            n_order1 += 1

            if self.max_order >= 2:
                for n2 in range(len(psi)):
                    j2 = psi[n2]['j']
                    if(j1 < j2):
                        U_2_c = subsample_fourier(cdgmm(U_1_c, psi[n2][j1]), k=2 ** (j2-j1))
                        U_2_c = fft(U_2_c, 'C2C', inverse=True)
                        U_2_c = fft(modulus(U_2_c), 'C2C')

                        # Third low pass filter
                        U_3_c = subsample_fourier(cdgmm(U_2_c, phi[j2]), k=2 ** (J-j2))
                        U_J_r = fft(U_3_c, 'C2R')

                        S[..., n_order2, :, :] = unpad(U_J_r)
                        n_order2 += 1

                        ### YST addition ###
                        if self.max_order >= 3:
                            for n3 in range(len(psi)):
                                j3 = psi[n3]['j']
                                if(j2 < j3):
                                    U_3_c = subsample_fourier(cdgmm(U_2_c, psi[n3][j2]), k=2 ** (j3-j2))
                                    U_3_c = fft(U_3_c, 'C2C', inverse=True)
                                    U_3_c = fft(modulus(U_3_c), 'C2C')

                                    # Fourth low pass filter
                                    U_4_c = subsample_fourier(cdgmm(U_3_c, phi[j3]), k=2 ** (J-j3))
                                    U_J_r = fft(U_4_c, 'C2R')

                                    S[..., n_order3, :, :] = unpad(U_J_r)
                                    n_order3 += 1


                                    ### YST addition ###
                                    if self.max_order >= 4:
                                        for n4 in range(len(psi)):
                                            j4 = psi[n4]['j']
                                            if(j3 < j4):
                                                U_4_c = subsample_fourier(cdgmm(U_3_c, psi[n4][j3]), k=2 ** (j4-j3))
                                                U_4_c = fft(U_4_c, 'C2C', inverse=True)
                                                U_4_c = fft(modulus(U_4_c), 'C2C')

                                                # Fourth low pass filter
                                                U_4_c = subsample_fourier(cdgmm(U_4_c, phi[j4]), k=2 ** (J-j4))
                                                U_J_r = fft(U_4_c, 'C2R')

                                                S[..., n_order4, :, :] = unpad(U_J_r)
                                                n_order4 += 1

        scattering_shape = S.shape[-3:]
        S = S.reshape(batch_shape + scattering_shape)

        return S

    def __call__(self, input):
        return self.forward(input)
