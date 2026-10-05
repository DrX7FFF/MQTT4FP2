## Aqara FP2 vers MQTT

Passerelle Linux native HomeKit/HAP vers MQTT, sans Home Assistant ni conteneur. Python 3.12 est requis. Le mapping utilise les caractéristiques HAP, les noms de service exposés et les valeurs réellement renvoyées par le FP2. Après un changement de zones, la gateway relit la liste des accessories et reconstruit ce mapping.

### Installation locale

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p data
cp .env.example .env
set -a
. ./.env
set +a
```

Renseignez les variables de broker dans `.env`. Lancez `pair.py` une seule fois pour l'appairage, puis `dump.py` pour inspecter ce que le FP2 expose avant de démarrer la gateway :

```sh
.venv/bin/python3 pair.py
.venv/bin/python3 dump.py
.venv/bin/python3 gateway.py
```


```sh
python pair.py
python dump.py
python gateway.py
```

La saisie du code HomeKit est masquée. Le pairing contient des clés privées : conservez le fichier hors du contrôle de version.

### Configuration

Variables disponibles : `MQTT_HOST` (défaut `127.0.0.1`), `MQTT_PORT` (`1883`), `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_PREFIX` (`fp2`), `PAIRING_FILE` (`data/pairing.json`), `PAIRING_ALIAS` (`fp2`), `LOG_LEVEL` (`INFO`), `DIAGNOSTIC_INTERVAL` (`30` s) et `FALLBACK_INTERVAL` (`60` s).

### Topics

| Topic | Payload |
| --- | --- |
| `<prefix>/status` | `online` ou `offline`, retain et Last Will |
| `<prefix>/presence` | JSON `{ "value": ..., "timestamp": ... }` |
| `<prefix>/illuminance` | JSON `{ "value": ..., "timestamp": ... }` |
| `<prefix>/zone/<nom>` | JSON `{ "value": ..., "timestamp": ... }` |
| `<prefix>/diagnostic/mqtt` | État, broker, séquence et QoS 1; publication retenue après confirmation du broker |
| `<prefix>/diagnostic/heartbeat` | Battement retenu de la gateway |
| `<prefix>/diagnostic/homekit` | État de connexion HomeKit |
| `<prefix>/diagnostic/mapping` | Entités reconnues, éléments non résolus et ambiguïtés |
| `<prefix>/diagnostic/last_event` | Dernier événement HAP reçu |
| `<prefix>/diagnostic/characteristic/<aid>/<iid>` | Valeur d'une caractéristique non mappée |

Les caractéristiques inconnues ou ambiguës restent visibles dans les topics de diagnostic; le service ne suppose pas les noms réels des zones. `diagnostic/mqtt` est publié en QoS 1, que `aiomqtt` confirme après le PUBACK du broker. Dans Node-RED, abonnez-vous à `<prefix>/diagnostic/#` et `<prefix>/status` pour vérifier la liaison.

### Tests

La configuration de pairing dans `tests/fixtures/pairing.json` est fictive et ne se connecte à aucun appareil. Lancez les tests hors ligne avec :

```sh
python -m unittest discover -s tests -v
```
