import warnings

from keras.src import backend
from keras.src import constraints
from keras.src import dtype_policies
from keras.src import initializers
from keras.src import ops
from keras.src import quantizers
from keras.src import regularizers
from keras.src.api_export import keras_export
from keras.src.layers.layer import Layer


@keras_export("keras.layers.Embedding")
class Embedding(Layer):
    """Turns positive integers (indexes) into dense vectors of fixed size.

    e.g. `[[4], [20]] -> [[0.25, 0.1], [0.6, -0.2]]`

    This layer can only be used on positive integer inputs of a fixed range.

    Example:

    >>> model = keras.Sequential()
    >>> model.add(keras.layers.Embedding(1000, 64))
    >>> # The model will take as input an integer matrix of size (batch,
    >>> # input_length), and the largest integer (i.e. word index) in the input
    >>> # should be no larger than 999 (vocabulary size).
    >>> # Now model.output_shape is (None, 10, 64), where `None` is the batch
    >>> # dimension.
    >>> input_array = np.random.randint(1000, size=(32, 10))
    >>> model.compile('rmsprop', 'mse')
    >>> output_array = model.predict(input_array)
    >>> print(output_array.shape)
    (32, 10, 64)

    Args:
        input_dim: Integer. Size of the vocabulary,
            i.e. maximum integer index + 1.
        output_dim: Integer. Dimension of the dense embedding.
        embeddings_initializer: Initializer for the `embeddings`
            matrix (see `keras.initializers`).
        embeddings_regularizer: Regularizer function applied to
            the `embeddings` matrix (see `keras.regularizers`).
        embeddings_constraint: Constraint function applied to
            the `embeddings` matrix (see `keras.constraints`).
        mask_zero: Boolean, whether or not the input value 0 is a special
            "padding" value that should be masked out.
            This is useful when using recurrent layers which
            may take variable length input. If this is `True`,
            then all subsequent layers in the model need
            to support masking or an exception will be raised.
            If `mask_zero` is set to `True`, as a consequence,
            index 0 cannot be used in the vocabulary (`input_dim` should
            equal size of vocabulary + 1).
        weights: Optional floating-point matrix of size
            `(input_dim, output_dim)`. The initial embeddings values
            to use.
        lora_rank: Optional integer. If set, the layer's forward pass
            will implement LoRA (Low-Rank Adaptation)
            with the provided rank. LoRA sets the layer's embeddings
            matrix to non-trainable and replaces it with a delta over the
            original matrix, obtained via multiplying two lower-rank
            trainable matrices. This can be useful to reduce the
            computation cost of fine-tuning large embedding layers.
            You can also enable LoRA on an existing
            `Embedding` layer by calling `layer.enable_lora(rank)`.

    Input shape:
        2D tensor with shape: `(batch_size, input_length)`.

    Output shape:
        3D tensor with shape: `(batch_size, input_length, output_dim)`.
    """

    def __init__(
        self,
        input_dim,
        output_dim,
        embeddings_initializer="uniform",
        embeddings_regularizer=None,
        embeddings_constraint=None,
        mask_zero=False,
        weights=None,
        lora_rank=None,
        **kwargs,
    ):
        input_length = kwargs.pop("input_length", None)
        if input_length is not None:
            warnings.warn(
                "Argument `input_length` is deprecated. Just remove it."
            )
        super().__init__(**kwargs)
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.embeddings_initializer = initializers.get(embeddings_initializer)
        self.embeddings_regularizer = regularizers.get(embeddings_regularizer)
        self.embeddings_constraint = constraints.get(embeddings_constraint)
        self.mask_zero = mask_zero
        self.supports_masking = mask_zero
        self.autocast = False
        self.lora_rank = lora_rank
        self.lora_enabled = False

        if weights is not None:
            self.build()
            if not (isinstance(weights, list) and len(weights) == 1):
                weights = [weights]
            self.set_weights(weights)

    def build(self, input_shape=None):
        if self.built:
            return
        # We use `self._dtype_policy` to check to avoid issues in torch dynamo
        is_quantized = isinstance(
            self._dtype_policy, dtype_policies.QuantizedDTypePolicy
        )
        if is_quantized:
            self.quantized_build(
                input_shape, mode=self.dtype_policy.quantization_mode
            )
        if not is_quantized or self.dtype_policy.quantization_mode != "int8":
            self._embeddings = self.add_weight(
                shape=(self.input_dim, self.output_dim),
                initializer=self.embeddings_initializer,
                name="embeddings",
                regularizer=self.embeddings_regularizer,
                constraint=self.embeddings_constraint,
                trainable=True,
            )
        self.built = True
        if self.lora_rank:
            self.enable_lora(self.lora_rank)

    @property
    def embeddings(self):
        if self.lora_enabled:
            return self._embeddings + ops.matmul(
                self.lora_embeddings_a, self.lora_embeddings_b
            )
        return self._embeddings

    def call(self, inputs):
        if inputs.dtype != "int32" and inputs.dtype != "int64":
            inputs = ops.cast(inputs, "int32")
        outputs = ops.take(self.embeddings, inputs, axis=0)
        return ops.cast(outputs, dtype=self.compute_dtype)

    def compute_mask(self, inputs, mask=None):
        if not self.mask_zero:
            return None
        return ops.not_equal(inputs, 0)

    def compute_output_shape(self, input_shape):
        return input_shape + (self.output_dim,)

    def enable_lora(
        self, rank, a_initializer="he_uniform", b_initializer="zeros"
    ):
        if self.embeddings_constraint:
            raise ValueError(
                "Lora is incompatible with embedding constraints. "
                "In order to enable lora on this layer, remove the "
                "`embeddings_constraint` argument."
            )
        if not self.built:
            raise ValueError(
                "Cannot enable lora on a layer that isn't yet built."
            )
        if self.lora_enabled:
            raise ValueError(
                "lora is already enabled. "
                "This can only be done once per layer."
            )
        self._tracker.unlock()
        self.lora_embeddings_a = self.add_weight(
            name="lora_embeddings_a",
            shape=(self.embeddings.shape[0], rank),
            initializer=initializers.get(a_initializer),
            regularizer=self.embeddings_regularizer,
        )
        self.lora_embeddings_b = self.add_weight(
            name="lora_embeddings_b",
            shape=(rank, self.embeddings.shape[1]),
            initializer=initializers.get(b_initializer),
            regularizer=self.embeddings_regularizer,
        )
        self.embeddings.trainable = False
        self._tracker.lock()
        self.lora_enabled = True
        self.lora_rank = rank

    def save_own_variables(self, store):
        # Do nothing if the layer isn't yet built
        if not self.built:
            return
        # The keys of the `store` will be saved as determined because the
        # default ordering will change after quantization
        embeddings_value, embeddings_scale = (
            self._get_embeddings_with_merged_lora()
        )
        store["0"] = embeddings_value
        if isinstance(self.dtype_policy, dtype_policies.QuantizedDTypePolicy):
            mode = self.dtype_policy.quantization_mode
            if mode == "int8":
                store["1"] = embeddings_scale
            else:
                raise NotImplementedError(
                    self.QUANTIZATION_MODE_ERROR_TEMPLATE.format(mode)
                )

    def load_own_variables(self, store):
        if not self.lora_enabled:
            self._check_load_own_variables(store)
        # Do nothing if the layer isn't yet built
        if not self.built:
            return
        # The keys of the `store` will be saved as determined because the
        # default ordering will change after quantization
        self._embeddings.assign(store["0"])
        if isinstance(self.dtype_policy, dtype_policies.QuantizedDTypePolicy):
            mode = self.dtype_policy.quantization_mode
            if mode == "int8":
                self.embeddings_scale.assign(store["1"])
            else:
                raise NotImplementedError(
                    self.QUANTIZATION_MODE_ERROR_TEMPLATE.format(mode)
                )
        if self.lora_enabled:
            self.lora_embeddings_a.assign(
                ops.zeros(self.lora_embeddings_a.shape)
            )
            self.lora_embeddings_b.assign(
                ops.zeros(self.lora_embeddings_b.shape)
            )

    def get_config(self):
        base_config = super().get_config()
        config = {
            "input_dim": self.input_dim,
            "output_dim": self.output_dim,
            "embeddings_initializer": initializers.serialize(
                self.embeddings_initializer
            ),
            "embeddings_regularizer": regularizers.serialize(
                self.embeddings_regularizer
            ),
            "activity_regularizer": regularizers.serialize(
                self.activity_regularizer
            ),
            "embeddings_constraint": constraints.serialize(
                self.embeddings_constraint
            ),
            "mask_zero": self.mask_zero,
        }
        if self.lora_rank:
            config["lora_rank"] = self.lora_rank
        return {**base_config, **config}

    def _check_load_own_variables(self, store):
        all_vars = self._trainable_variables + self._non_trainable_variables
        if len(store.keys()) != len(all_vars):
            if len(all_vars) == 0 and not self.built:
                raise ValueError(
                    f"Layer '{self.name}' was never built "
                    "and thus it doesn't have any variables. "
                    f"However the weights file lists {len(store.keys())} "
                    "variables for this layer.\n"
                    "In most cases, this error indicates that either:\n\n"
                    "1. The layer is owned by a parent layer that "
                    "implements a `build()` method, but calling the "
                    "parent's `build()` method did NOT create the state of "
                    f"the child layer '{self.name}'. A `build()` method "
                    "must create ALL state for the layer, including "
                    "the state of any children layers.\n\n"
                    "2. You need to implement "
                    "the `def build_from_config(self, config)` method "
                    f"on layer '{self.name}', to specify how to rebuild "
                    "it during loading. "
                    "In this case, you might also want to implement the "
                    "method that generates the build config at saving time, "
                    "`def get_build_config(self)`. "
                    "The method `build_from_config()` is meant "
                    "to create the state "
                    "of the layer (i.e. its variables) upon deserialization.",
                )
            raise ValueError(
                f"Layer '{self.name}' expected {len(all_vars)} variables, "
                "but received "
                f"{len(store.keys())} variables during loading. "
                f"Expected: {[v.name for v in all_vars]}"
            )

    """Quantization-related (int8) methods"""

    QUANTIZATION_MODE_ERROR_TEMPLATE = (
        "Invalid quantization mode. Expected 'int8'. "
        "Received: quantization_mode={mode}"
    )

    def quantized_build(self, input_shape, mode):
        if mode == "int8":
            self._int8_build()
        else:
            raise NotImplementedError(
                self.QUANTIZATION_MODE_ERROR_TEMPLATE.format(mode)
            )

    def _int8_build(
        self,
        embeddings_initializer="zeros",
        embeddings_scale_initializer="ones",
    ):
        self.inputs_quantizer = quantizers.AbsMaxQuantizer(axis=-1)
        self._embeddings = self.add_weight(
            name="embeddings",
            shape=(self.input_dim, self.output_dim),
            initializer=embeddings_initializer,
            dtype="int8",
            trainable=False,
        )
        self.embeddings_scale = self.add_weight(
            name="embeddings_scale",
            shape=(self.output_dim,),
            initializer=embeddings_scale_initializer,
            trainable=False,
        )

    def quantized_call(self, inputs):
        if self.dtype_policy.quantization_mode == "int8":
            return self._int8_call(inputs)
        else:
            mode = self.dtype_policy.quantization_mode
            raise NotImplementedError(
                self.QUANTIZATION_MODE_ERROR_TEMPLATE.format(mode)
            )

    def _int8_call(self, inputs):
        # We cannot update quantized self._embeddings, so the custom gradient is
        # not needed
        if backend.standardize_dtype(inputs.dtype) not in ("int32", "int64"):
            inputs = ops.cast(inputs, "int32")
        outputs = ops.take(self._embeddings, inputs, axis=0)
        # De-scale outputs
        outputs = ops.cast(outputs, self.compute_dtype)
        outputs = ops.divide(
            outputs, ops.expand_dims(self.embeddings_scale, axis=0)
        )
        if self.lora_enabled:
            lora_outputs = ops.take(self.lora_embeddings_a, inputs, axis=0)
            lora_outputs = ops.matmul(lora_outputs, self.lora_embeddings_b)
            outputs = ops.add(outputs, lora_outputs)
        return outputs

    def quantize(self, mode):
        import gc

        # Prevent quantization of the subclasses
        if type(self) is not Embedding:
            raise NotImplementedError(
                f"Layer {self.__class__.__name__} does not have a `quantize()` "
                "method implemented."
            )
        self._check_quantize_args(mode, self.compute_dtype)

        # Set new dtype policy
        if not isinstance(
            self.dtype_policy, dtype_policies.QuantizedDTypePolicy
        ):
            quantized_dtype = f"{mode}_from_{self.dtype_policy.name}"
            # We set the internal `self._dtype_policy` instead of using the
            # setter to avoid double `quantize` call
            self._dtype_policy = dtype_policies.get(quantized_dtype)

        self._tracker.unlock()
        if mode == "int8":
            if backend.standardize_dtype(self._embeddings.dtype) == "int8":
                raise ValueError("`quantize` can only be done once per layer.")
            # Configure `self.inputs_quantizer`
            self.inputs_quantizer = quantizers.AbsMaxQuantizer(axis=-1)
            # Quantize `self._embeddings` to int8 and compute corresponding
            # scale
            embeddings_value, embeddings_scale = quantizers.abs_max_quantize(
                self._embeddings, axis=0
            )
            embeddings_scale = ops.squeeze(embeddings_scale, axis=0)
            self._untrack_variable(self._embeddings)
            del self._embeddings
            # Utilize a lambda expression as an initializer to prevent adding a
            # large constant to the computation graph.
            self._int8_build(
                lambda shape, dtype: embeddings_value,
                lambda shape, dtype: embeddings_scale,
            )
        else:
            raise NotImplementedError(
                self.QUANTIZATION_MODE_ERROR_TEMPLATE.format(mode)
            )
        self._tracker.lock()

        # Release memory manually because sometimes the backend doesn't
        gc.collect()

    def _get_embeddings_with_merged_lora(self):
        if isinstance(self.dtype_policy, dtype_policies.QuantizedDTypePolicy):
            embeddings_value = self._embeddings
            embeddings_scale = self.embeddings_scale
            if self.lora_enabled:
                # Dequantize & quantize to merge lora weights into embeddings
                # Note that this is a lossy compression
                embeddings_value = ops.divide(
                    embeddings_value, embeddings_scale
                )
                embeddings_value = ops.add(
                    embeddings_value,
                    ops.matmul(self.lora_embeddings_a, self.lora_embeddings_b),
                )
                embeddings_value, embeddings_scale = (
                    quantizers.abs_max_quantize(embeddings_value, axis=0)
                )
                embeddings_scale = ops.squeeze(embeddings_scale, axis=0)
            return embeddings_value, embeddings_scale
        return self.embeddings, None
