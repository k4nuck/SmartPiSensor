#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  ecobee_hk.py
#
# Shared aiohomekit controller setup for the pairing CLI and the service.

from contextlib import asynccontextmanager

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
		await controller.async_stop()
		await browser.async_cancel()
		await azc.async_close()
