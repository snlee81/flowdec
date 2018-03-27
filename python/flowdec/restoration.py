""" Deconvolution algorithm implementations """
import abc
import tensorflow as tf
from tfdecon.fft_utils_tf import OPM_LOG2, OPM_NONE, optimize_dims, fftshift, ifftshift
from tfdecon.tf_ops import pad_around_center, unpad_around_center, tf_print
from tfdecon import fft_utils_tf


class DeconvolutionResult(object):

    def __init__(self, data, info):
        self.data = data
        self.info = info

class DeconvolutionGraph(object):

    def __init__(self, tf_graph, inputs, outputs):
        self.tf_graph = tf_graph
        self.inputs = inputs
        self.outputs = outputs

    def save(self, export_dir, save_as_text=True):
        signature = tf.saved_model.signature_def_utils.build_signature_def(
            inputs={k: tf.saved_model.utils.build_tensor_info(v) for k, v in self.inputs.items()},
            outputs = {k: tf.saved_model.utils.build_tensor_info(v) for k, v in self.outputs.items()}
        )

        builder = tf.saved_model.builder.SavedModelBuilder(export_dir)

        with tf.Session(graph=self.tf_graph) as sess:
            builder.add_meta_graph_and_variables(
                sess, [tf.saved_model.tag_constants.SERVING],
                signature_def_map={tf.saved_model.signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY: signature}
            )
        builder.save(as_text=save_as_text)
        return self



class Deconvolver(metaclass=abc.ABCMeta):

    def _get_tf_graph(self):
        graph = tf.Graph()
        with graph.as_default():
            inputs, outputs = self._build_tf_graph()
        return DeconvolutionGraph(graph, inputs, outputs)

    def initialize(self):
        self.graph = self._get_tf_graph()
        return self

    @abc.abstractmethod
    def run(self):
        raise NotImplementedError()

    def _run(self, acquisition, input_kwargs, session_config=None):

        if not hasattr(self, 'graph'):
            raise ValueError('Must initialize deconvolver before running (via `.initialize` function)')

        with tf.Session(config=session_config, graph=self.graph.tf_graph) as sess:
            data_dict = {self.graph.inputs[k]:v for k, v in acquisition.to_feed_dict().items()}
            args_dict = {self.graph.inputs[k]:v for k, v in input_kwargs.items()}
            res = sess.run(self.graph.outputs, feed_dict={**data_dict, **args_dict})
            return res

    def _run_batch(self, acquisition_batch, **kwargs):
        return [self._run(acq, **kwargs) for acq in acquisition_batch]


def default_input_prep_fn(tensor_name, tensor):
    """ Prepare Kernel/PSF by normalizing sum to one
    Args:
        tensor_name: Name of tensor to apply function to
        tensor: Tensor value
    Returns:
        Normalized PSF tensor
    """
    if tensor_name.startswith('kernel:'):
        return tensor / tf.reduce_sum(tensor)
    return tensor


class FFTDeconvolver(Deconvolver):

    def __init__(self, n_dims, pad_mode=OPM_LOG2, input_prep_fn=default_input_prep_fn, output_prep_fn=None,
                 real_domain_fft=False, epsilon=1e-6):
        """FFT-based Deconvolution Algorithm

        Args:
            n_dims: Rank of tensors to be used as inputs (i.e. number of dimensions)
            pad_mode: Padding mode for optimal FFT performance (defaults to powers of 2 i.e. 'log2' )
            input_prep_fn: Data preparation function to inject within computation graph.  Default is PSF normalization
                function used to ensure PSF tensor sums to one
            output_prep_fn: Output preparation function to inject within computation graph (e.g.
                Clipping values in deconvolved results); signature is fn(tensor, inputs=None) where
                input placeholders may be provided as a way to make transformations of results dependent on
                input data (inputs is a dictionary keyed by tensor input name)
            real_domain_fft: Flag indicating whether or not to use the real or complex TF FFT functions
            epsilon: Minimum value below which interemdiate results will become 0 to avoid division by small numbers
        """
        self.n_dims = n_dims
        self.pad_mode = pad_mode
        self.input_prep_fn = input_prep_fn
        self.output_prep_fn = output_prep_fn
        self.real_domain_fft = real_domain_fft
        self.fft_dtype = tf.float32 if real_domain_fft else tf.complex64
        self.epsilon = epsilon

        # Because TF FFT implementations all only work with 32-bit floats the spatial inputs/outputs in the
        # constructed graph are constrained to this type for now (but it could change in the future)
        self.dtype = tf.float32

    def _wrap_input(self, tensor):
        return self.input_prep_fn(tensor.name, tensor) if self.input_prep_fn else tensor

    def _wrap_output(self, tensor, inputs=None):
        return self.output_prep_fn(tensor.name, tensor, inputs=inputs) if self.output_prep_fn else tensor


class FFTIterativeDeconvolver(FFTDeconvolver):

    def _get_niter(self):
        return tf.placeholder(tf.int32, shape=(), name='niter')


def richardson_lucy(acquisition, niter=10, pad_mode=OPM_LOG2, session_config=None, **kwargs):
    algo = RichardsonLucyDeconvolver(acquisition.data.ndim, pad_mode=pad_mode, **kwargs)
    return algo.initialize().run(acquisition, niter, session_config=session_config).data


class RichardsonLucyDeconvolver(FFTIterativeDeconvolver):
    """Richardson-Lucy Deconvolution

    *Note: Comments throughout are in reference to the following implementations:

    Reference Implementations:
        - Matlab: https://svn.ecdf.ed.ac.uk/repo/ph/IGM/matlab/generic/images/deconvlucy.m
        - Basic Matlab: https://en.wikipedia.org/wiki/Talk:Richardson%E2%80%93Lucy_deconvolution
        - Scikit-Image: https://github.com/scikit-image/scikit-image/blob/master/skimage/restoration/deconvolution.py
        - DeconvolutionLab2: https://github.com/hadim/DeconvolutionLab2/blob/jcufft/src/main/java/deconvolution/
        algorithm/RichardsonLucy.java

    """

    def run(self, acquisition, niter, session_config=None):
        res = self._run(
            acquisition, dict(niter=niter, pad_mode=self.pad_mode), 
            session_config=session_config)
        return DeconvolutionResult(res['result'], info=None)

    def _build_tf_graph(self):
        niter = self._get_niter()
        padmh = tf.placeholder(tf.string, name='padding_mode')
        datah = self._wrap_input(tf.placeholder(self.dtype, shape=[None] * self.n_dims, name='data'))
        kernh = self._wrap_input(tf.placeholder(self.dtype, shape=[None] * self.n_dims, name='kernel'))

        # Add assertion operations to validate padding mode and data/kernel dimensions
        flag_pad_mode = tf.stack([tf.equal(padmh, OPM_LOG2), tf.equal(padmh, OPM_NONE)], axis=0)
        assert_pad_mode = tf.assert_greater(
                tf.reduce_sum(tf.cast(flag_pad_mode, tf.int32)), 0,
                message='Pad mode not valid', data=[padmh])

        flag_shapes = tf.shape(datah) - tf.shape(kernh)
        assert_shapes = tf.assert_greater_equal(
                tf.reduce_sum(flag_shapes), 0,
                message='Data shape must be >= kernel shape', data=[tf.shape(datah), tf.shape(kernh)])

        with tf.control_dependencies([assert_pad_mode, assert_shapes]):
            # If configured to do so, expand dimensions (and pad) data matrix to power of 2 to avoid use of
            # Bluestein algorithm in favor of significantly faster Cooley-Tukey FFT
            datat = tf.cond(
                tf.equal(padmh, OPM_LOG2),
                lambda: pad_around_center(datah, optimize_dims(tf.shape(datah), OPM_LOG2)),
                lambda: datah
            )

            # Pad kernel to equal dimensions of data tensor
            kernt = tf.cast(ifftshift(pad_around_center(kernh, tf.shape(datat))), self.fft_dtype)

        # Infer available TF FFT functions based on predefined number of data dimensions
        fft_fwd, fft_rev = fft_utils_tf.get_fft_tf_fns(self.n_dims, real_domain_only=self.real_domain_fft)

        # Determine intermediate kernel representation necessary based on domain specified to carry out computations
        kern_fft = fft_fwd(kernt)
        if self.real_domain_fft:
            kern_fft_conj = fft_fwd(tf.reverse(kernt, axis=tf.range(0, self.n_dims)))
        else:
            kern_fft_conj = tf.conj(kern_fft)

        # Initialize resulting deconvolved image -- there are several sensible choices for this like the original image
        # or constant arrays, but some experiments show this to be better, and other implementations doing the same are
        # "Basic Matlab" and "Scikit-Image" (see class notes for links)
        decon = .5 * tf.ones_like(datat, dtype=self.dtype)

        def cond(i, decon):
            return i <= niter

        def conv(data, kernel_fft):
            return tf.real(fft_rev(fft_fwd(tf.cast(data, self.fft_dtype)) * kernel_fft))

        def body(i, decon):
            # Richardson-Lucy Iteration - logic taken largely from a combination of
            # the scikit-image (real domain) and DeconvolutionLab2 implementations (complex domain)
            conv1 = conv(decon, kern_fft)

            # High-pass filter to avoid division by very small numbers (see DeconvolutionLab2)
            blur1 = tf.where(conv1 < self.epsilon, tf.zeros_like(datat), datat / conv1)

            conv2 = conv(blur1, kern_fft_conj)

            # Positivity constraint on result for iteration
            decon = tf.maximum(decon * conv2, 0.)

            # i = tf_print(i)
            return i + 1, decon

        result = tf.while_loop(cond, body, [1, decon], parallel_iterations=1)[1]

        # Crop off any padding that may have been added to reach more efficient dimension sizes
        result = unpad_around_center(result, tf.shape(datah))

        # Wrap output in configured post-processing functions (if any)
        result = tf.identity(self._wrap_output(result, {'data': datah, 'kernel': kernh}), name='result')

        inputs = {'niter': niter, 'data': datah, 'kernel': kernh, 'pad_mode': padmh}
        outputs = {'result': result}

        return inputs, outputs

