from .fully_connected_model import FullyConnectedModel
from .linear_cnn_model import LinearConv1Model
from .nonlinear_cnn_model import NonlinearConv2Model
from .linear_rnn_model import LinearRNNModel
from .model import AbstractModel
from .bert import Bert

from .model import OutputEncoding, InputEncoding, HyperParameter

from .combined_model import combine_models


_models = (
    FullyConnectedModel,
    LinearConv1Model,
    NonlinearConv2Model,
    LinearRNNModel,
    Bert
)
models = {cls.__name__: cls for cls in _models}
del _models

