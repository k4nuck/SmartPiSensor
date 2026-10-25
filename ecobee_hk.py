#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  ecobee_hk.py
#
# Shared aiohomekit controller setup for the pairing CLI and the service.

import asyncio
import logging
from contextlib import asynccontextmanager

# Each teardown step is bounded so a stuck socket can't carry the service past systemd's stop deadline.
TEARDOWN_STEP_SECONDS = 2

async def _bounded(step, awaitable):
	try:
		await asyncio.wait_for(awaitable, TEARDOWN_STEP_SECONDS)
	except Exception as error:
		logging.warning("Ecobee:teardown %s did not finish cleanly: %r", step, error)

@asynccontextmanager
async def open_controller():
	from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
	from aiohomekit.characteristic_cache import CharacteristicCacheMemory
	from aiohomekit.controller.ip.controller import IpController
	from aiohomekit.zeroconf import HAP_TYPE_TCP

	azc = AsyncZeroconf()
	# aiohomekit only attaches to a browser the caller already started; it never creates one itself.
	browser = AsyncServiceBrowser(azc.zeroconf, [HAP_TYPE_TCP], handlers=[lambda **kwargs: None])
	# IpController, not the umbrella Controller: skips the BLE/CoAP backends, which a Pi 3 with 120M doesn't need.
	controller = IpController(char_cache=CharacteristicCacheMemory(), zeroconf_instance=azc)
	try:
		await controller.async_start()
		yield controller
	finally:
		await _bounded("controller", controller.async_stop())
		await _bounded("browser", browser.async_cancel())
		await _bounded("zeroconf", azc.async_close())
