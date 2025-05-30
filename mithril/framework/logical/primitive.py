# Copyright 2022 Synnada, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ... import types
from .base import BaseKey, BaseModel, ConnectionDataType
from .model import Model
from .operator import Operator

__all__ = ["PrimitiveModel", "OperatorModel"]

ConstantType = float | int | types.Constant


class OperatorModel(Model):
    def __init__(
        self,
        model: Operator,
        *,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name, enforce_jit=model._jittable)
        self._extend(model, {k: k for k in model.external_keys}, trace=False)
        self.expose_keys(*model.external_keys)
        self._freeze()

    @property
    def submodel(self) -> Operator:
        m = next(iter(self.dag.keys()))
        assert isinstance(m, Operator)
        return m

    def extend(
        self,
        model: BaseModel,
        trace: bool = True,
        /,
        **kwargs: ConnectionDataType,
    ) -> None:
        if len(self.dag) > 0:
            raise RuntimeError("Primitive models cannot have submodels.")
        super().extend(model, trace, **kwargs)


class PrimitiveModel(OperatorModel):
    def __init__(
        self,
        formula_key: str,
        *,
        name: str | None = None,
        **kwargs: BaseKey,
    ) -> None:
        model = Operator(formula_key, self.class_name, **kwargs)
        super().__init__(model=model, name=name)
