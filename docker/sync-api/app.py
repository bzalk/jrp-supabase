#!/usr/bin/env python3
from pathlib import Path
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_api.audit as _audit
import sync_api.branches as _branches
import sync_api.branch_core as _branch_core
import sync_api.branch_ops as _branch_ops
import sync_api.branch_services as _branch_services
import sync_api.branch_snapshot as _branch_snapshot
import sync_api.compare as _compare
import sync_api.database as _database
import sync_api.database_collect as _database_collect
import sync_api.database_sql as _database_sql
import sync_api.db as _db
import sync_api.db_core as _db_core
import sync_api.db_sql as _db_sql
import sync_api.edge_functions as _edge_functions
import sync_api.env_config as _env_config
import sync_api.http_utils as _http_utils
import sync_api.import_plan as _import_plan
import sync_api.management as _management
import sync_api.migration_display as _migration_display
import sync_api.migrations as _migrations
import sync_api.openapi as _openapi
import sync_api.operations as _operations
import sync_api.restore_filter as _restore_filter
import sync_api.server as _server
import sync_api.settings as _settings

from sync_api.settings import *
from sync_api.audit import *
from sync_api.management import *
from sync_api.env_config import *
from sync_api.http_utils import *
from sync_api.import_plan import *
from sync_api.openapi import *
from sync_api.db import *
from sync_api.branches import *
from sync_api.database import *
from sync_api.edge_functions import *
from sync_api.operations import *
from sync_api.server import SyncApiHandler, main


_COMPONENT_MODULES = (
    _settings,
    _audit,
    _management,
    _env_config,
    _http_utils,
    _import_plan,
    _openapi,
    _db,
    _db_core,
    _db_sql,
    _restore_filter,
    _branches,
    _branch_core,
    _branch_services,
    _branch_snapshot,
    _branch_ops,
    _database,
    _database_sql,
    _database_collect,
    _migration_display,
    _migrations,
    _compare,
    _edge_functions,
    _operations,
    _server,
)


class _SyncApiFacade(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for module in _COMPONENT_MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


if __name__ in sys.modules:
    sys.modules[__name__].__class__ = _SyncApiFacade

if __name__ == "__main__":
    main()
