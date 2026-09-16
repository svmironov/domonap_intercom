# Несколько Rubetek Panel аккаунтов

Home Assistant может одновременно держать две и более независимые записи Domonap/Rubetek Panel.

Каждая запись интеграции имеет собственные:

- access/refresh token;
- `instanceId`/panel identity;
- SignalR consumer;
- состояние активного вызова;
- временный Domonap SIP account;
- опциональный SIP account на Asterisk;
- namespace entity `unique_id`.

Это позволяет, например, подключить разные квартиры, парковочные места или другие доступы как отдельные записи интеграции.

## Почему нужен `config_entry_id`

`DoorId` или `KeyId` не всегда достаточно для выбора аккаунта. Одна и та же физическая дверь может быть доступна нескольким учётным записям, а разные записи могут иметь разные ключи для разных дверей.

Поэтому services интеграции принимают опциональный `config_entry_id`:

```yaml
action: domonap.open_relay_by_door_id
data:
  door_id: "REPLACE_WITH_DOOR_ID"
  config_entry_id: "REPLACE_WITH_CONFIG_ENTRY_ID"
```

При одном аккаунте параметр часто можно не указывать. При двух и более аккаунтах его рекомендуется передавать всегда, особенно в Telegram callbacks и ручных меню дверей.

Panel-событие `domonap_incoming_call` содержит `config_entry_id`, поэтому для кнопки открытия двери по текущему звонку не нужно угадывать аккаунт:

```text
SignalR event
  ├─ config_entry_id
  └─ DoorId
        ↓
Telegram callback / automation
        ↓
domonap.open_relay_by_door_id
  ├─ config_entry_id
  └─ door_id
```

## Где посмотреть `config_entry_id`

### Через Developer Tools → Template

Возьмите любую entity, которая точно принадлежит нужной записи Domonap:

```jinja
{{ config_entry_id('binary_sensor.REPLACE_WITH_ENTITY_ID') }}
```

или камеру:

```jinja
{{ config_entry_id('camera.REPLACE_WITH_ENTITY_ID') }}
```

### Через `.storage/core.config_entries`

Из каталога конфигурации Home Assistant:

```bash
jq -r '
  .data.entries[]
  | select(.domain == "domonap")
  | [.entry_id, .title]
  | @tsv
' .storage/core.config_entries
```

Вы получите список вида:

```text
01EXAMPLEENTRY00000000000001    Panel A
01EXAMPLEENTRY00000000000002    Panel B
```

Не редактируйте `.storage/core.config_entries` вручную. Команда выше нужна только для чтения.

## Где взять `DoorId`

Самый надёжный вариант — слушать событие `domonap_incoming_call` в **Developer Tools → Events**. Поле `DoorId` относится к двери, с которой пришёл звонок, а `config_entry_id` показывает нужную запись интеграции.

Для ручного меню дверей можно один раз собрать соответствие:

```yaml
doors:
  door_a:
    door_id: "REPLACE_WITH_DOOR_ID_A"
    entry_id: "REPLACE_WITH_CONFIG_ENTRY_ID_A"

  door_b:
    door_id: "REPLACE_WITH_DOOR_ID_B"
    entry_id: "REPLACE_WITH_CONFIG_ENTRY_ID_B"
```

Не пытайтесь определять аккаунт только по `DoorId` в automation: при нескольких записях это может привести к открытию через неверную сессию или к ошибке `No panel key found for DoorId`.

## Asterisk при нескольких аккаунтах

Для каждого Panel entry рекомендуется отдельный SIP User на Asterisk:

```text
Panel A → SIP user domonap-a ─┐
                              ├→ Asterisk → extension 100
Panel B → SIP user domonap-b ─┘
```

Номер назначения (`call number`) может быть одинаковым. Разные SIP User упрощают регистрацию, диагностику и исключают коллизии Contact/AoR.

## Одновременные звонки

Сами runtime записи изолированы и могут одновременно получать разные SignalR/SIP вызовы. Но automation/service без `config_entry_id` становится неоднозначной, если активно больше одного звонка.

Поэтому для multi-account automation правило простое:

**сохраняйте и передавайте `config_entry_id` вместе с `DoorId` от события до финального service call.**

Готовый обезличенный пример: [../examples/automation.yaml](../examples/automation.yaml).


## Выбор аккаунта и миграция устройств

Если подключён один аккаунт или звонок активен ровно у одного аккаунта,
сервисы могут выбрать его автоматически. В остальных случаях необходимо
передать `config_entry_id`: произвольный первый аккаунт больше не выбирается.
Явно указанный датчик `entity_id` должен принадлежать выбранному аккаунту.
Поиск датчика последнего звонка использует реестр сущностей и работает после
переименования датчика.

События звонков телефонного профиля также содержат `config_entry_id`.
Идентификаторы устройств разделены по аккаунтам. При первом запуске после
обновления миграция сохраняет ID устройств с одним владельцем. Если одно
устройство ранее объединяло несколько аккаунтов, оно разделяется: сущности
сохраняют свои `entity_id`, но часть получает новый `device_id`. Автоматизации,
ссылающиеся именно на старое общее устройство, следует привязать к нужному
аккаунту; ссылки на сущности сохраняются.
