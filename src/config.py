from pathlib import Path

from yaml import FullLoader, load

BASE_DIR = Path(__file__).parent.parent
SRC_DIR = BASE_DIR / "src"
STATIC_DIR = BASE_DIR / "static"
DEFAULT_CONFIG_FILE = BASE_DIR / "config.yml"


class Config:
    @staticmethod
    def get_config() -> dict:
        with Path(SRC_DIR, DEFAULT_CONFIG_FILE).open() as f:
            return load(f, Loader=FullLoader)  # noqa: S506

    @staticmethod
    def reload() -> None:
        Config.get_config.cache_clear()


config = Config().get_config()
