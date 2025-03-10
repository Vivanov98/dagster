from contextlib import ExitStack
from typing import Dict, Mapping, Optional, Type, cast

from dagster._core.definitions.assets import AssetsDefinition
from dagster._core.definitions.events import AssetKey, CoercibleToAssetKey
from dagster._core.definitions.resource_definition import ResourceDefinition
from dagster._core.definitions.utils import DEFAULT_IO_MANAGER_KEY
from dagster._core.execution.build_resources import build_resources, get_mapped_resource_config
from dagster._core.execution.context.input import build_input_context
from dagster._core.execution.context.output import build_output_context
from dagster._core.instance import DagsterInstance
from dagster._core.types.dagster_type import resolve_dagster_type
from dagster._utils import merge_dicts

from .fs_io_manager import fs_io_manager
from .io_manager import IOManager


class AssetValueLoader:
    """Caches resource definitions that are used to load asset values across multiple load
    invocations"""

    def __init__(
        self,
        assets_defs_by_key: Mapping[AssetKey, AssetsDefinition],
        instance: Optional[DagsterInstance] = None,
    ):
        self._assets_defs_by_key = assets_defs_by_key
        self._resource_instance_cache: Dict[str, object] = {}
        self._instance = instance
        self._exit_stack: ExitStack = ExitStack().__enter__()

    def _ensure_resource_instances_in_cache(self, resource_defs: Mapping[str, ResourceDefinition]):
        for built_resource_key, built_resource in (
            self._exit_stack.enter_context(
                build_resources(
                    resources={
                        resource_key: self._resource_instance_cache.get(resource_key, resource_def)
                        for resource_key, resource_def in resource_defs.items()
                    },
                    instance=self._instance,
                )
            )
            ._asdict()
            .items()
        ):
            self._resource_instance_cache[built_resource_key] = built_resource

    def load_asset_value(
        self,
        asset_key: CoercibleToAssetKey,
        python_type: Optional[Type] = None,
    ) -> object:
        """
        Loads the contents of an asset as a Python object.

        Invokes `load_input` on the :py:class:`IOManager` associated with the asset.

        Args:
            asset_key (Union[AssetKey, Sequence[str], str]): The key of the asset to load.
            python_type (Optional[Type]): The python type to load the asset as. This is what will
                be returned inside `load_input` by `context.dagster_type.typing_type`.

        Returns:
            The contents of an asset as a Python object.
        """
        asset_key = AssetKey.from_coerceable(asset_key)

        assets_def = self._assets_defs_by_key[asset_key]
        resource_defs = merge_dicts(
            {DEFAULT_IO_MANAGER_KEY: fs_io_manager}, assets_def.resource_defs
        )
        io_manager_key = assets_def.get_io_manager_key_for_asset_key(asset_key)
        io_manager_def = resource_defs[io_manager_key]
        required_resource_keys = io_manager_def.required_resource_keys | {io_manager_key}

        self._ensure_resource_instances_in_cache(
            {k: v for k, v in resource_defs.items() if k in required_resource_keys}
        )
        io_manager = cast(IOManager, self._resource_instance_cache[io_manager_key])

        io_manager_config = get_mapped_resource_config({io_manager_key: io_manager_def}, {})

        input_context = build_input_context(
            name=None,
            asset_key=asset_key,
            dagster_type=resolve_dagster_type(python_type),
            upstream_output=build_output_context(metadata=assets_def.metadata_by_key[asset_key]),
            resources=self._resource_instance_cache,
            resource_config=io_manager_config[io_manager_key].config,
        )

        return io_manager.load_input(input_context)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._exit_stack.close()
