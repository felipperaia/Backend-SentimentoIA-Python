from functools import lru_cache
from importlib import import_module
from typing import Any


def _split_loader_path(loader_path: str) -> tuple[str, str]:
    module_name, separator, attr_name = str(loader_path or "").partition(":")
    if not separator or not module_name or not attr_name:
        raise ValueError(f"Loader invalido: {loader_path!r}")
    return module_name, attr_name


@lru_cache(maxsize=128)
def load_object(loader_path: str) -> Any:
    module_name, attr_name = _split_loader_path(loader_path)
    module = import_module(module_name)
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        raise ValueError(f"Atributo '{attr_name}' nao encontrado em '{module_name}'") from exc

