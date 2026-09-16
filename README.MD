# Domonap Home Assistant Integration

![Logo](https://github.com/svmironov/domonap_intercom/blob/main/img/icon.png?raw=true)

Интеграция позволяет управлять устройствами, привязанными к приложению **Domonap**, из **Home Assistant**.

## Возможности

- Открытие дверей.
- Загрузка видеопотока.
- Локальный WebRTC/WHEP proxy для `go2rtc` без передачи авторизации Domonap в `go2rtc`.
- Уведомления о звонках в виде бинарного сенсора.
- События входящих звонков и сообщений для автоматизаций.
- Исходная авторизация **номер телефона + SMS**.
- Отдельный профиль **Rubetek Panel** с 8-значным кодом активации или импортом существующей panel-сессии.
- Одновременная работа двух и более Rubetek Panel записей.
- Опциональная интеграция Rubetek Panel с внешним SIP/Asterisk.

## Документация

- [Авторизация Rubetek Panel](docs/rubetek-panel.md) — откуда взять 8-значный код, активация и импорт существующей panel-сессии.
- [SIP / Asterisk](docs/sip.md) — схема сигнализации, RTP, DTMF, NAT и завершение вызова.
- [Несколько Rubetek Panel аккаунтов](docs/multi-account.md) — `config_entry_id`, разделение дверей и отдельные SIP accounts.
- [Пример automation.yaml](docs/automation-example.md) — описание всех значений для обезличенного примера.
- [`examples/automation.yaml`](examples/automation.yaml) — готовый обезличенный multi-account Telegram пример без реальных ID и токенов.

## Авторизация

При добавлении новой записи интеграции можно выбрать один из двух профилей.

### Номер телефона + SMS

Это исходный режим интеграции. Существующие записи, созданные до появления Rubetek Panel, продолжают использовать его без миграции на новый профиль.

### Rubetek Panel

Rubetek/AOSP Panel использует отдельный panel principal и не требует авторизации Home Assistant по номеру телефона.

Для новой панели требуется **8-значный код активации**. Его нужно запросить через поддержку в официальном приложении Domonap, указав, что требуется код активации стационарной/Rubetek-панели для вашего объекта. Код используется только один раз для обмена на panel-сессию и не сохраняется в Home Assistant.

Если panel-сессия уже была активирована и сохранена, её можно импортировать как JSON без повторного использования кода.

Подробная инструкция: [docs/rubetek-panel.md](docs/rubetek-panel.md).

## Несколько аккаунтов

Каждая Rubetek Panel запись имеет собственные токены, SignalR consumer, состояние вызова и SIP runtime. Entity `unique_id` для Panel записей изолированы по `config_entry_id`, поэтому разные аккаунты могут одновременно существовать в одном Home Assistant.

Для service calls при двух и более аккаунтах рекомендуется всегда передавать `config_entry_id`:

```yaml
action: domonap.open_relay_by_door_id
data:
  door_id: "REPLACE_WITH_DOOR_ID"
  config_entry_id: "REPLACE_WITH_CONFIG_ENTRY_ID"
```

Panel-событие `domonap_incoming_call` также содержит `config_entry_id`. Его следует передавать вместе с `DoorId` до callback/service call, а не пытаться угадывать аккаунт только по двери.

Подробнее: [docs/multi-account.md](docs/multi-account.md).

## SIP / Asterisk

Для Rubetek Panel можно включить внешний SIP в options записи интеграции. Home Assistant регистрируется на настроенном SIP server и при входящем Domonap вызове звонит на указанный extension.

Home Assistant мостит **только SIP-сигнализацию**:

```text
                 SIP                     SIP
Domonap  <----------------> Home Assistant <----------------> Asterisk
   │                                                     │
   └──────────────────── RTP напрямую ──────────────────┘
```

Исходный SDP Domonap передаётся Asterisk без изменения, а SDP answer Asterisk передаётся обратно Domonap без изменения. NAT, RTP, codec negotiation и transcoding должны быть настроены на Asterisk.

Жизненный цикл вызова повторяет приложение Domonap:

- открытие двери всегда завершает вызов; перед открытием реле активная SIP-сессия принимается (200 OK), что останавливает звонок панели домофона — без звука через Home Assistant;
- неудачное открытие тоже завершает уже принятый вызов, чтобы он не висел без открытой двери;
- вызов ограничен 60 секундами (счётчик стартует при входящем вызове и перезапускается при ответе), как `EndCallTimer` в приложении;
- если вызов ответил другой жилец (`DomofonCallAnswered`), Home Assistant завершает свои SIP-сессии, кроме случая, когда абонент Asterisk уже ответил;
- REST `NotifyCallEnded` отправляется только как fallback, когда временная SIP-регистрация не удалась — при живой регистрации завершение сигнализируется через SIP BYE/reject, как `endCallSmart()` в приложении.

В настройках записи интеграции доступен режим завершения вызова (`call_end_mode`):

- `answer` (по умолчанию) — «взять трубку и положить»: перед открытием двери вызов принимается (200 OK, панель замолкает), после — BYE;
- `reject` — «сбросить»: открытие и завершение идут без ответа (603 Decline, панель продолжает звонить другим). Полезно при нестабильном SIP: путь короче и быстрее. Сервис `silence_active_call` в любом режиме принимает вызов — его задача именно замолчать панель.

Сейчас поддерживается UDP на внешнем SIP-соединении к Asterisk. Для DTMF `1` направление Asterisk → Home Assistant должно использовать SIP INFO (`dtmf_mode=info`). Успешное открытие двери через DTMF, Home Assistant или Telegram завершает текущий вызов на обоих SIP-диалогах, повторяя поведение приложения.

> SIP-функциональность пока экспериментальная: на отдельных вызовах завершение временной Domonap SIP-сессии может вести себя нестабильно. Для диагностики используйте SIP logger Asterisk и DEBUG-логи интеграции.

Подробнее: [docs/sip.md](docs/sip.md).

## WebRTC proxy для go2rtc

Интеграция поднимает локальный WHEP proxy в Home Assistant. Он сам обновляет токен и проксирует WebRTC-сессию в Domonap, поэтому `go2rtc` может подключаться к HA URL без авторизации в Domonap.

У WebRTC-камеры появляются атрибуты:

- `go2rtc_webrtc_path` — относительный путь proxy внутри Home Assistant;
- `go2rtc_webrtc_url` — полный URL для подключения, если в HA настроен внутренний или внешний URL.

Пример `go2rtc`:

```yaml
streams:
  domonap_entrance:
    - webrtc:http://HOME_ASSISTANT:8123/api/domonap/webrtc_proxy/<secret>/<camera_id>/whep
```

Практически удобнее брать готовое значение из атрибута `go2rtc_webrtc_url` у сущности камеры и вставлять его в конфиг `go2rtc`.

## События и автоматизации

### `domonap_incoming_call`

При входящем звонке интеграция генерирует событие примерно следующего вида:

```yaml
event_type: domonap_incoming_call
data:
  EventMessage: DomofonCalling
  DoorId: "REDACTED_DOOR_ID"
  Address: "Домофон"
  CallId: "REDACTED_CALL_ID"
  VideoUrl: "https://hls.domonap.ru/..."
  HttpVideoUrl: "https://hls.domonap.ru/..."
  VideoPreview: "https://api.domonap.ru/..."
  SipAccount: "REDACTED"
  SipPassword: "REDACTED"
  SipDomain: "REDACTED"
  SipPort: "REDACTED"
  PushType: Domofon
  config_entry_id: "REDACTED_CONFIG_ENTRY_ID"
  PhotoUrl: "https://home-assistant.local/api/domonap/media_proxy/..."
  photoUrl: "https://home-assistant.local/api/domonap/media_proxy/..."
  OriginalPhotoUrl: "https://s3-api.domonap.ru/..."
origin: LOCAL
```

Основные поля:

- `DoorId` — идентификатор двери.
- `Address` — адрес/название вызывающего устройства.
- `config_entry_id` — для Rubetek Panel показывает, какая именно запись интеграции получила вызов; критически важно при multi-account.
- `PhotoUrl` / `photoUrl` — URL фотографии звонящего через локальный proxy Home Assistant.
- `OriginalPhotoUrl` — оригинальный `photoUrl` из истории звонков Domonap для диагностики.
- `PushPhotoUrl` — исходный `photoUrl` из push-события, если Domonap его прислал.
- `VideoPreview` — авторизованное превью вызова.
- `WebrtcVideoUrl` / `webrtcVideoUrl` — live WHEP-поток вызова из push-события (приложение использует его как превью входящего звонка).
- `DoorAddresses` — список дверей вызова (`doorId` + `address`), если Domonap прислал несколько.

### Другие события

Интеграция также поддерживает события сообщений и изменения статуса пользователей, включая:

- `domonap_receive_message`;
- `domonap_user_status_changed`.

### Ручное управление звонком

Два сервиса работают с активным звонком без открытия двери и поддерживают оба профиля:

```yaml
# Принять (SIP 200 OK) и сразу завершить — панель домофона замолкает,
# медиа через Home Assistant не проходит.
action: domonap.silence_active_call

# Завершить не отвечая (603 Decline) — наша ветка звонка прекращается,
# панель может продолжать звонить другим жильцам.
action: domonap.reject_active_call
```

Оба сервиса принимают опциональный `config_entry_id` (важно при нескольких аккаунтах) и возвращают ответ со статусом для автоматизаций.

### Приём вызова без разговора

Оба профиля воспроизводят поведение приложения: при открытии двери (сервисом или кнопкой) активная SIP-сессия домофона сначала **принимается** (200 OK с SDP без медиа — discard-порт), и именно это останавливает звонок панели. Затем вызов завершается: BYE после ответа или 603 Decline, если никто не отвечал. Голос/видео через Home Assistant не проходят никогда.

### Примеры

Минимальное уведомление в мобильное приложение Home Assistant:

```yaml
automation:
  - alias: "Уведомление о звонке"
    triggers:
      - trigger: event
        event_type: domonap_incoming_call
    actions:
      - action: notify.mobile_app
        data:
          message: "Входящий звонок на домофон"
          data:
            image: "{{ trigger.event.data.PhotoUrl }}"
```

Для Telegram, нескольких Rubetek аккаунтов, ручного меню дверей и Telegram topics используйте обезличенный пример [`examples/automation.yaml`](examples/automation.yaml). Откуда брать `DoorId`, `config_entry_id`, chat/thread ID и Telegram event entity описано в [docs/automation-example.md](docs/automation-example.md).

## Ограничения

### Phone/SMS profile

Открытие двери и `silence_active_call` принимают SIP-вызов (200 OK без медиа), поэтому панель домофона замолкает сразу — как в мобильном приложении.

Ограничение на одновременное использование одного номера телефона в официальном приложении Domonap и интеграции HA остаётся: на мобильное устройство могут перестать приходить фоновые push-уведомления о входящем звонке (SignalR-событие при этом продолжает приходить в HA).

Rubetek Panel использует отдельный профиль и отдельный SignalR runtime, поэтому это ограничение не является его моделью работы.

### SIP

Внешний SIP доступен только для Rubetek Panel и в текущей реализации поддерживает UDP. Завершение некоторых временных Domonap SIP-сессий всё ещё находится в стадии доработки.

## Установка

- Установите интеграцию через HACS или скопируйте `custom_components/domonap` вручную.
- Перезапустите Home Assistant.
- Добавьте интеграцию в **Настройки → Устройства и службы**.
- Выберите профиль авторизации:
  - **Номер телефона + SMS** для исходного мобильного режима;
  - **Rubetek Panel** для активации 8-значным кодом или импорта существующей panel-сессии.

## Безопасность

Не публикуйте:

- `accessToken` / `refreshToken`;
- JSON panel-сессии;
- `SipPassword` из входящего Domonap вызова;
- пароль SIP account Asterisk;
- содержимое `.storage/core.config_entries` целиком.

При публикации automation/log обязательно обезличивайте `DoorId`, `KeyId`, `config_entry_id`, Telegram chat/thread IDs и адреса объектов.

## Поддержка проекта

Если интеграция оказалась полезной, вы можете [поддержать проект](https://pay.cloudtips.ru/p/be41717c).

## Отказ от ответственности

Данное программное обеспечение никак не связано и не одобрено ООО «ДОМОНАП», владельцем торговой марки ДОМОНАП. Используйте его на свой страх и риск. Автор ни при каких обстоятельствах не несёт ответственности за повреждение или утрату вашего имущества, а также за возможный вред третьим лицам.

Все логотипы, названия брендов и продуктов принадлежат их законным владельцам.
