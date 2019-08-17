import logging

__all__ = ['Scattering2D']

class Scattering2D(object):
    def __init__(self, *args, **kwargs):
        if 'frontend' not in kwargs:
            frontend='numpy'
        else:
            frontend=kwargs['frontend']
            kwargs.pop('frontend')

        if frontend == 'numpy':
            try:
                from .numpy_frontend import Scattering2DNumpy
                self.__class__ = Scattering2DNumpy
                self.__init__(*args, **kwargs)
            except:
                raise
            logging.info('NumPy frontend is used.')
        elif frontend == 'torch':
            try:
                from .torch_frontend import Scattering2DTorch
                self.__class__ = Scattering2DTorch
                self.__init__(*args, **kwargs)
            except:
                raise
            logging.info('PyTorch frontend is used.')
        elif frontend == 'tensorflow':
            try:
                from .tensorflow_frontend import Scattering2DTensorflow
                self.__class__ = Scattering2DTensorflow
                self.__init__(*args, **kwargs)
            except:
                raise
            logging.info('TensorFlow frontend is used.')
        else:
            raise RuntimeError('This frontend is not available.')