"""Storage factory for creating different storage implementations.

Aztec fork uses ``LocalStorage`` exclusively — uploaded corpus zips
land in the ``uploads`` named volume mounted at
``settings.UPLOAD_FOLDER`` (see ``deployment/docker-compose-hub.yaml``).
The upstream S3 backend was removed alongside the admin SPA; if you
need it back, restore ``application/storage/s3.py`` and the
``boto3`` dependency in ``application/requirements.txt``.
"""
from typing import Dict, Type

from application.storage.base import BaseStorage
from application.storage.local import LocalStorage
from application.core.settings import settings


class StorageCreator:
    storages: Dict[str, Type[BaseStorage]] = {
        "local": LocalStorage,
    }
    
    _instance = None
    
    @classmethod
    def get_storage(cls) -> BaseStorage:
        if cls._instance is None:
            storage_type = getattr(settings, "STORAGE_TYPE", "local")
            cls._instance = cls.create_storage(storage_type)
        
        return cls._instance
    
    @classmethod
    def create_storage(cls, type_name: str, *args, **kwargs) -> BaseStorage:
        storage_class = cls.storages.get(type_name.lower())
        if not storage_class:
            raise ValueError(f"No storage implementation found for type {type_name}")
        
        return storage_class(*args, **kwargs)
