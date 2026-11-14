# -*- coding: utf-8 -*-
import traceback

from .setup import P, logger

try:
    from .mod_basic import ModuleBasic

    P.set_module_list([ModuleBasic])
    logger.info('[hdhomerun_ff] plugin module loaded')
except Exception as exc:  # pragma: no cover - exercised by FF at startup
    logger.error('[hdhomerun_ff] startup failed: %s', exc)
    logger.error(traceback.format_exc())
