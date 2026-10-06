# FP2 pour Node-RED : prototype minimal

Un noeud d'entree `fp2`, natif JavaScript, sans Python ni MQTT a l'execution.
Il reutilise un fichier d'appairage **aiohomekit IP** existant et ne realise
aucun nouvel appairage. Aucun dump n'est necessaire : les caracteristiques sont
lues directement sur le FP2 a chaque connexion.

## Installation

Node.js 18 minimum et une installation Node-RED existante sont requis.
Sur la machine qui execute Node-RED, depuis son dossier utilisateur
(generalement `~/.node-red`) :

```sh
npm install /chemin/vers/MQTT4FP2/node-red-contrib-fp2
```

Redemarrer Node-RED. Ajouter le noeud **FP2 zones** de la categorie des entrees
et le relier a un noeud **debug**, configure pour afficher le message complet.
Le module local doit rester accessible a Node-RED.

## Installation sous Docker / Linux

Placer le dossier du module dans le volume persistant pour qu'il soit accessible
dans le conteneur sous `/data/node-red-contrib-fp2`, et le pairing reel sous
`/data/fp2-pairing.json` (lisible par Node-RED).

/data/
├── node-red-contrib-fp2/
│   ├── package.json
│   ├── fp2.js
│   └── fp2.html
└── fp2-pairing.json


Dans une console **exec** du conteneur :

```sh
cd /data
npm install --ignore-scripts ./node-red-contrib-fp2
```

Redemarrer le conteneur. Dans Node-RED, ajouter **FP2 zones**, renseigner
**Pairing** avec `/data/fp2-pairing.json` et l'alias du fichier, puis relier
la sortie a un **debug** et deployer. Arreter auparavant la gateway Python.

`--ignore-scripts` evite la compilation Bluetooth inutile au transport IP.
Conserver le dossier du module dans `/data`; ne pas partager le pairing.

## Configuration

1. Arreter la gateway Python qui utilise la meme identite HomeKit.
2. Indiquer le chemin absolu du fichier `data/pairing.json` existant, accessible
   sur la machine Node-RED. Un chemin relatif part du dossier utilisateur Node-RED,
   pas du dossier de ce depot.
3. Indiquer l'alias present dans ce fichier (`fp2` par defaut).
4. Laisser adresse et port vides pour utiliser `AccessoryIP` et `AccessoryPort`,
   ou les remplacer si l'adresse a change. Ce port est le port HAP, pas MQTT.
5. Deployer et verifier le statut vert, puis entrer et sortir d'une zone.

Le fichier contient des cles privees : ne pas le publier ni le placer dans un
flow exporte. Les cles restent dans le fichier et ne sont jamais envoyees dans
les messages. Le module ne modifie pas ce fichier. Utiliser un seul noeud par FP2.

## Messages

```json
{
  "topic": "fp2/zone/bureau",
  "payload": true,
  "zone": "Bureau",
  "aid": "1",
  "iid": "10",
  "timestamp": "2026-10-06T12:00:00.000Z"
}
```

`true` signifie occupe, `false` signifie libre. Les identifiants sont des
chaines pour conserver les identifiants HAP sans perte de precision.
Le premier etat est memorise sans message. Ensuite, les valeurs repetees sont
ignorees et un message est emis par changement de presence. Si la lecture
initiale d'une caracteristique echoue, son premier evenement valide est emis.
Le timestamp est celui de reception dans Node-RED, pas celui du capteur.

Toutes les caracteristiques OccupancyDetected avec permission `ev` sont
incluses, meme la presence globale : le prototype ne suppose pas comment le
FP2 distingue celle-ci des zones. Les noms viennent de ConfiguredName ou Name;
sans nom, le topic utilise `zone-<aid>-<iid>`. Les noms identiques sont suffixes.

## Limites

- Pas de nouvel appairage, de decouverte mDNS ou de luminosite.
- Reconnexion sur erreur de connexion ou coupure des abonnements, avec attente
  progressive de 1 a 60 secondes. Une coupure silencieuse depend de sa detection
  par TCP : il n'y a pas de surveillance periodique dans ce prototype.
- Les zones sont relues lors d'une reconnexion. Apres une modification des zones,
  redeployer le noeud si la connexion reste ouverte.
- A la reconnexion, les etats sont reinitialises silencieusement. Les changements
  survenus pendant une coupure ne sont pas rejoues. Pas de retain ni de Last Will.
- La conversion des cles est testee hors ligne; la connexion et les notifications
  doivent encore etre validees sur un FP2 reel.

Le client est `hap-controller` 0.10.2. Seul son transport IP est charge, sans
demarrer le Bluetooth. Cette dependance inclut toutefois des composants BLE;
sur une machine sans outils de compilation, leur installation peut poser probleme.

## Tests hors ligne

Depuis ce dossier :

```sh
npm install --ignore-scripts
npm test
```

`--ignore-scripts` evite la compilation des composants Bluetooth inutilises pour
ces tests IP. Les tests utilisent exclusivement le pairing fictif et un client
simule; ils ne contactent aucun appareil. Les tests cryptographiques utilisent
la vraie bibliotheque JavaScript.