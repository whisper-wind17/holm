"""Administrator display names, independent of Runtime routing names."""


class InvalidInstanceName(RuntimeError):
    """A supplied display name cannot be stored under the naming contract."""


def normalize_instance_name(value: str) -> str:
    name = value.strip()
    if "\x00" in name or not 2 <= len(name) <= 64:
        raise InvalidInstanceName()
    return name


def default_instance_name(model_name: str, sequence: int = 1) -> str:
    """Display-only candidate; the repository arbitrates uniqueness."""
    if sequence < 1:
        raise ValueError("sequence must be positive")
    base = model_name.rsplit("/", 1)[-1].strip()
    if len(base) < 2:
        base = "模型实例"
    suffix = "" if sequence == 1 else f"-{sequence}"
    return base[: 64 - len(suffix)].rstrip() + suffix
