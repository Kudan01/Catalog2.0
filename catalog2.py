#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import time

if os.environ.get("CATALOG2_DIAGNOSTICS", "").strip().lower() in {"1", "true", "yes", "on"}:
    os.environ["CATALOG2_DIAGNOSTIC_SCRIPT_START_NS"] = str(time.perf_counter_ns())

from catalog_app.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
