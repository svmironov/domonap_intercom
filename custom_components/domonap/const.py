from homeassistant.const import Platform

import homeassistant.helpers.config_validation as cv
import voluptuous as vol


DOMAIN = 'domonap'
API = "api"
CALL_CONTROLLER = "call_controller"
CONF_COUNTRY_CODE = "country_code"
CONF_PHONE_NUMBER = "phone_number"
CONF_CONFIRM_CODE = "confirm_code"
CONF_AUTH_MODE = "auth_mode"
CONF_PANEL_SETUP_MODE = "panel_setup_mode"
CONF_PANEL_SESSION = "panel_session"

AUTH_MODE_PHONE = "phone"
AUTH_MODE_PANEL = "panel"
PANEL_SETUP_CODE = "activation_code"
PANEL_SETUP_SESSION = "existing_session"

PARAM_ACCESS_TOKEN = "access_token"
PARAM_REFRESH_TOKEN = "refresh_token"
PARAM_REFRESH_EXPIRATION = "refresh_expiration_date"
PARAM_DEVICE_TOKEN = "device_token"
PARAM_INSTANCE_ID = "instance_id"
PARAM_WEBRTC_PROXY_SECRET = "webrtc_proxy_secret"
PARAM_AUTH_MODE = "auth_mode"
PARAM_PANEL_USER_ID = "panel_user_id"
PARAM_PANEL_NAME = "panel_name"
PARAM_PANEL_DEVICE_INFO = "panel_device_info"

OPT_EXTERNAL_SIP_ENABLED = "external_sip_enabled"
OPT_EXTERNAL_SIP_USER = "external_sip_user"
OPT_EXTERNAL_SIP_PASSWORD = "external_sip_password"
OPT_EXTERNAL_SIP_DOMAIN = "external_sip_domain"
OPT_EXTERNAL_SIP_TRANSPORT = "external_sip_transport"
OPT_EXTERNAL_SIP_CALL_NUMBER = "external_sip_call_number"
EXTERNAL_SIP_TRANSPORT_UDP = "udp"

OPT_CALL_END_MODE = "call_end_mode"
CALL_END_MODE_ANSWER = "answer"
CALL_END_MODE_REJECT = "reject"

EVENT_INCOMING_CALL = "domonap_incoming_call"
EVENT_CALL_ANSWERED = "domonap_call_answered"
EVENT_CALL_ENDED = "domonap_call_ended"
WEBRTC_PROXY = "webrtc_proxy"
MEDIA_PROXY = "media_proxy"

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.CAMERA, Platform.BINARY_SENSOR, Platform.SENSOR, Platform.IMAGE]

RESET_DELAY = 10 # секунды

# Phone/SMS SignalR transport. The current app (v9851) connects with
# shouldSkipNegotiate(true): a direct WebSocket to the hub, no negotiate and
# no connectionToken query parameter.
WS_MESSAGE_END = "\x1e"
WS_HANDSHAKE_MESSAGE = '{"protocol":"json","version":1}' + WS_MESSAGE_END
WS_PING_MESSAGE = '{"type":6}' + WS_MESSAGE_END
WS_URL = "wss://api.domonap.ru/notificationHub"

# Rubetek/AOSP panel transport recovered from the APK: direct WebSocket,
# shouldSkipNegotiate(true), no connectionToken query parameter.
PANEL_WS_URL = "wss://api.domonap.ru/notificationHub"
PANEL_WS_KEEPALIVE_INTERVAL = 3
PANEL_WS_SERVER_TIMEOUT = 300
PANEL_WS_HANDSHAKE_TIMEOUT = 100
PANEL_WS_RECONNECT_INITIAL = 2
PANEL_WS_RECONNECT_MAX = 60

# SignalR keep-alive параметры старого phone/SMS клиента.
WS_KEEPALIVE_INTERVAL = 15  # секунды
WS_SERVER_TIMEOUT = 30  # секунды