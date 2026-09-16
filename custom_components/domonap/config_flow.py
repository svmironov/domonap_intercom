from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol
import re
from secrets import token_urlsafe
from typing import Any, Optional

from .const import (
    DOMAIN,
    CONF_COUNTRY_CODE,
    CONF_PHONE_NUMBER,
    CONF_CONFIRM_CODE,
    CONF_AUTH_MODE,
    CONF_PANEL_SETUP_MODE,
    CONF_PANEL_SESSION,
    AUTH_MODE_PHONE,
    AUTH_MODE_PANEL,
    PANEL_SETUP_CODE,
    PANEL_SETUP_SESSION,
    PARAM_REFRESH_EXPIRATION,
    PARAM_REFRESH_TOKEN,
    PARAM_ACCESS_TOKEN,
    PARAM_WEBRTC_PROXY_SECRET,
    PARAM_DEVICE_TOKEN,
    PARAM_INSTANCE_ID,
    PARAM_AUTH_MODE,
    PARAM_PANEL_USER_ID,
    PARAM_PANEL_NAME,
    PARAM_PANEL_DEVICE_INFO,
    OPT_EXTERNAL_SIP_ENABLED,
    OPT_EXTERNAL_SIP_USER,
    OPT_EXTERNAL_SIP_PASSWORD,
    OPT_EXTERNAL_SIP_DOMAIN,
    OPT_EXTERNAL_SIP_TRANSPORT,
    OPT_EXTERNAL_SIP_CALL_NUMBER,
    EXTERNAL_SIP_TRANSPORT_UDP,
    OPT_CALL_END_MODE,
    CALL_END_MODE_ANSWER,
    CALL_END_MODE_REJECT,
)
from .api import IntercomAPI, is_android_guid
from .external_sip_signaling import parse_host_port
from .panel_api import RubetekPanelIntercomAPI


class IntercomFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    # Keep schema version 1: entries created by main remain valid and default to
    # the original phone/SMS profile when auth_mode is absent.
    VERSION = 1

    def __init__(self):
        self._auth_mode = AUTH_MODE_PHONE
        self._country_code = None
        self._phone_number = None
        self._confirm_code = None
        self._api = IntercomAPI()
        self._reauth_entry = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return IntercomOptionsFlow(config_entry)

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._auth_mode = entry_data.get(PARAM_AUTH_MODE, AUTH_MODE_PHONE)

        if self._auth_mode == AUTH_MODE_PANEL:
            return await self.async_step_panel_setup()

        self._country_code = entry_data.get(CONF_COUNTRY_CODE)
        self._phone_number = entry_data.get(CONF_PHONE_NUMBER)
        stored_device_token = entry_data.get(PARAM_DEVICE_TOKEN)
        self._api = IntercomAPI(
            device_token=(
                stored_device_token if is_android_guid(stored_device_token) else None
            ),
            instance_id=entry_data.get(PARAM_INSTANCE_ID),
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            if not self._country_code or not self._phone_number:
                self._auth_mode = AUTH_MODE_PHONE
                return await self.async_step_phone()
            response = await self._send_authorization_code()
            if response is not True:
                errors["base"] = "authorization_failed"
            else:
                return await self.async_step_confirm()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_user(self, user_input=None):
        """Choose the authorization profile for a new integration entry."""
        if user_input is not None:
            self._auth_mode = user_input[CONF_AUTH_MODE]
            if self._auth_mode == AUTH_MODE_PANEL:
                return await self.async_step_panel_setup()
            self._api = IntercomAPI()
            return await self.async_step_phone()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AUTH_MODE, default=AUTH_MODE_PHONE
                    ): vol.In([AUTH_MODE_PHONE, AUTH_MODE_PANEL])
                }
            ),
        )

    async def async_step_phone(self, user_input=None):
        """Original phone + SMS authorization path."""
        errors = {}
        if user_input is not None:
            self._country_code = self._sanitize_number(user_input[CONF_COUNTRY_CODE])
            self._phone_number = self._sanitize_number(user_input[CONF_PHONE_NUMBER])

            response = await self._send_authorization_code()
            if response is not True:
                errors["base"] = "authorization_failed"
            else:
                return await self.async_step_confirm()

        data_schema = vol.Schema({
            vol.Required(CONF_COUNTRY_CODE): str,
            vol.Required(CONF_PHONE_NUMBER): str,
        })

        return self.async_show_form(
            step_id="phone", data_schema=data_schema, errors=errors
        )

    async def async_step_confirm(self, user_input=None):
        """Original SMS confirmation path, isolated from panel auth."""
        errors = {}
        if user_input is not None:
            self._confirm_code = user_input[CONF_CONFIRM_CODE]

            response = await self._api.confirm_authorization(
                self._country_code, self._phone_number, self._confirm_code
            )
            if (
                not self._api.access_token
                or not self._api.refresh_token
                or (
                    isinstance(response, dict)
                    and ("errorText" in response or "error" in response)
                )
            ):
                errors["base"] = "confirmation_failed"
            else:
                data = self._entry_data_phone()
                title = "+" + self._country_code + " " + self._phone_number
                if self._reauth_entry is not None:
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        title=title,
                        data=data,
                    )
                    await self.hass.config_entries.async_reload(
                        self._reauth_entry.entry_id
                    )
                    return self.async_abort(reason="reauth_successful")
                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Required(CONF_CONFIRM_CODE): str}),
            errors=errors,
        )

    async def async_step_panel_setup(self, user_input=None):
        """Choose fresh code activation or restore an already activated panel."""
        if user_input is not None:
            setup_mode = user_input[CONF_PANEL_SETUP_MODE]
            if setup_mode == PANEL_SETUP_SESSION:
                return await self.async_step_panel_session()
            instance_id = (
                self._reauth_entry.data.get(PARAM_INSTANCE_ID)
                if self._reauth_entry is not None
                else None
            )
            device_info = (
                self._reauth_entry.data.get(PARAM_PANEL_DEVICE_INFO)
                if self._reauth_entry is not None
                else None
            )
            self._api = RubetekPanelIntercomAPI(
                instance_id=instance_id,
                device_info=device_info,
            )
            return await self.async_step_panel()

        return self.async_show_form(
            step_id="panel_setup",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PANEL_SETUP_MODE, default=PANEL_SETUP_CODE
                    ): vol.In([PANEL_SETUP_CODE, PANEL_SETUP_SESSION])
                }
            ),
        )

    async def async_step_panel(self, user_input=None):
        """Rubetek panel provisioning-code flow."""
        errors = {}
        if user_input is not None:
            confirm_code = self._sanitize_number(user_input[CONF_CONFIRM_CODE])
            if len(confirm_code) != 8:
                errors["base"] = "invalid_panel_code"
            else:
                if not isinstance(self._api, RubetekPanelIntercomAPI):
                    self._api = RubetekPanelIntercomAPI()
                response = await self._api.confirm_panel_authorization(confirm_code)
                if (
                    not self._api.access_token
                    or not self._api.refresh_token
                    or (
                        isinstance(response, dict)
                        and ("errorText" in response or "error" in response)
                    )
                ):
                    errors["base"] = "panel_confirmation_failed"
                else:
                    return await self._finish_panel_flow()

        return self.async_show_form(
            step_id="panel",
            data_schema=vol.Schema({vol.Required(CONF_CONFIRM_CODE): str}),
            errors=errors,
        )

    async def async_step_panel_session(self, user_input=None):
        """Restore a Panel principal from a previously captured session JSON."""
        errors = {}
        if user_input is not None:
            try:
                self._api = RubetekPanelIntercomAPI.from_session_payload(
                    user_input[CONF_PANEL_SESSION]
                )
            except (TypeError, ValueError, KeyError):
                errors["base"] = "invalid_panel_session"
            else:
                return await self._finish_panel_flow()

        return self.async_show_form(
            step_id="panel_session",
            data_schema=vol.Schema({vol.Required(CONF_PANEL_SESSION): str}),
            errors=errors,
        )

    async def _finish_panel_flow(self):
        data = self._entry_data_panel()
        panel_name = self._api.panel.get("name") or "Rubetek Panel"
        if self._reauth_entry is not None:
            self.hass.config_entries.async_update_entry(
                self._reauth_entry,
                title=panel_name,
                data=data,
            )
            await self.hass.config_entries.async_reload(
                self._reauth_entry.entry_id
            )
            return self.async_abort(reason="reauth_successful")
        return self.async_create_entry(title=panel_name, data=data)

    def _sanitize_number(self, input_string):
        return re.sub(r'\D', '', input_string)

    async def _send_authorization_code(self):
        return await self._api.authorize(self._country_code, self._phone_number)

    def _entry_data_phone(self) -> dict[str, Optional[str]]:
        data = dict(self._reauth_entry.data) if self._reauth_entry is not None else {}
        data.setdefault(PARAM_WEBRTC_PROXY_SECRET, token_urlsafe(24))
        data.update(
            {
                PARAM_AUTH_MODE: AUTH_MODE_PHONE,
                PARAM_ACCESS_TOKEN: self._api.access_token,
                PARAM_REFRESH_TOKEN: self._api.refresh_token,
                PARAM_REFRESH_EXPIRATION: self._api.refresh_expiration_date,
                PARAM_DEVICE_TOKEN: self._api.device_token,
                PARAM_INSTANCE_ID: self._api.instance_id,
                CONF_COUNTRY_CODE: self._country_code,
                CONF_PHONE_NUMBER: self._phone_number,
            }
        )
        data.pop(PARAM_PANEL_USER_ID, None)
        data.pop(PARAM_PANEL_NAME, None)
        data.pop(PARAM_PANEL_DEVICE_INFO, None)
        return data

    def _entry_data_panel(self) -> dict[str, Optional[str]]:
        data = dict(self._reauth_entry.data) if self._reauth_entry is not None else {}
        data.setdefault(PARAM_WEBRTC_PROXY_SECRET, token_urlsafe(24))
        data.update(
            {
                PARAM_AUTH_MODE: AUTH_MODE_PANEL,
                PARAM_ACCESS_TOKEN: self._api.access_token,
                PARAM_REFRESH_TOKEN: self._api.refresh_token,
                PARAM_REFRESH_EXPIRATION: self._api.refresh_expiration_date,
                PARAM_INSTANCE_ID: self._api.instance_id,
                PARAM_PANEL_DEVICE_INFO: self._api.device_info,
                PARAM_PANEL_USER_ID: self._api.panel.get("userId"),
                PARAM_PANEL_NAME: self._api.panel.get("name"),
            }
        )
        data.pop(PARAM_DEVICE_TOKEN, None)
        data.pop(CONF_COUNTRY_CODE, None)
        data.pop(CONF_PHONE_NUMBER, None)
        return data


class IntercomOptionsFlow(config_entries.OptionsFlow):
    """Options that change runtime behavior but not Domonap authorization."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        is_panel = (
            self._config_entry.data.get(PARAM_AUTH_MODE, AUTH_MODE_PHONE)
            == AUTH_MODE_PANEL
        )
        options = self._config_entry.options
        errors = {}
        if user_input is not None:
            enabled = bool(user_input.get(OPT_EXTERNAL_SIP_ENABLED, False))
            if is_panel and enabled:
                if not user_input.get(OPT_EXTERNAL_SIP_USER, "").strip():
                    errors["base"] = "external_sip_user_required"
                elif not user_input.get(OPT_EXTERNAL_SIP_DOMAIN, "").strip():
                    errors["base"] = "external_sip_domain_required"
                elif not user_input.get(OPT_EXTERNAL_SIP_CALL_NUMBER, "").strip():
                    errors["base"] = "external_sip_number_required"
                else:
                    try:
                        parse_host_port(user_input[OPT_EXTERNAL_SIP_DOMAIN])
                    except (TypeError, ValueError):
                        errors["base"] = "external_sip_domain_invalid"
            if not errors:
                return self.async_create_entry(title="", data=dict(user_input))

        schema = {
            vol.Required(
                OPT_CALL_END_MODE,
                default=options.get(OPT_CALL_END_MODE, CALL_END_MODE_ANSWER),
            ): vol.In([CALL_END_MODE_ANSWER, CALL_END_MODE_REJECT]),
        }
        if is_panel:
            schema.update(
                {
                    vol.Required(
                        OPT_EXTERNAL_SIP_ENABLED,
                        default=options.get(OPT_EXTERNAL_SIP_ENABLED, False),
                    ): bool,
                    vol.Optional(
                        OPT_EXTERNAL_SIP_USER,
                        default=options.get(OPT_EXTERNAL_SIP_USER, ""),
                    ): str,
                    vol.Optional(
                        OPT_EXTERNAL_SIP_PASSWORD,
                        default=options.get(OPT_EXTERNAL_SIP_PASSWORD, ""),
                    ): str,
                    vol.Optional(
                        OPT_EXTERNAL_SIP_DOMAIN,
                        default=options.get(OPT_EXTERNAL_SIP_DOMAIN, ""),
                    ): str,
                    vol.Optional(
                        OPT_EXTERNAL_SIP_TRANSPORT,
                        default=options.get(
                            OPT_EXTERNAL_SIP_TRANSPORT, EXTERNAL_SIP_TRANSPORT_UDP
                        ),
                    ): vol.In([EXTERNAL_SIP_TRANSPORT_UDP]),
                    vol.Optional(
                        OPT_EXTERNAL_SIP_CALL_NUMBER,
                        default=options.get(OPT_EXTERNAL_SIP_CALL_NUMBER, ""),
                    ): str,
                }
            )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
        )
