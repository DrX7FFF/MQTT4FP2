"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");

function convertPairing(pairing) {
    if (!pairing || pairing.Connection !== "IP") {
        throw new Error("Un appairage aiohomekit IP est requis.");
    }
    for (const field of ["AccessoryPairingID", "iOSPairingId"]) {
        if (typeof pairing[field] !== "string" || !pairing[field]) {
            throw new Error(`Champ d'appairage absent : ${field}`);
        }
    }
    for (const field of ["AccessoryLTPK", "iOSDeviceLTPK", "iOSDeviceLTSK"]) {
        const length = field === "iOSDeviceLTSK" ? "(?:[0-9a-f]{64}|[0-9a-f]{128})" : "[0-9a-f]{64}";
        if (typeof pairing[field] !== "string" || !new RegExp(`^${length}$`, "i").test(pairing[field])) {
            throw new Error(`Cle d'appairage invalide : ${field}`);
        }
    }
    return {
        AccessoryPairingID: Buffer.from(pairing.AccessoryPairingID, "utf8").toString("hex"),
        AccessoryLTPK: pairing.AccessoryLTPK,
        iOSDevicePairingID: Buffer.from(pairing.iOSPairingId, "utf8").toString("hex"),
        iOSDeviceLTPK: pairing.iOSDeviceLTPK,
        iOSDeviceLTSK: pairing.iOSDeviceLTSK.length === 64
            ? pairing.iOSDeviceLTSK + pairing.iOSDeviceLTPK
            : pairing.iOSDeviceLTSK,
    };
}

function shortType(value) {
    return String(value || "").toUpperCase().split("-")[0].replace(/^0+/, "");
}

function slug(name) {
    return name.normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
        .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function buildZones(database) {
    const zones = new Map();
    const usedSlugs = new Set();
    for (const accessory of database.accessories || []) {
        for (const service of accessory.services || []) {
            const characteristics = service.characteristics || [];
            const nameValue = ["E3", "23"].map(type => characteristics.find(characteristic =>
                shortType(characteristic.type) === type && typeof characteristic.value === "string"
                && characteristic.value.trim())).find(Boolean);
            for (const characteristic of characteristics) {
                if (shortType(characteristic.type) !== "71" || !characteristic.perms?.includes("ev")) {
                    continue;
                }
                const aid = String(accessory.aid);
                const iid = String(characteristic.iid);
                const name = nameValue ? nameValue.value.trim() : `zone-${aid}-${iid}`;
                let zone = slug(name) || `zone-${aid}-${iid}`;
                if (usedSlugs.has(zone)) {
                    zone = `${zone}-${aid}-${iid}`;
                }
                usedSlugs.add(zone);
                zones.set(`${aid}.${iid}`, { aid, iid, name, zone });
            }
        }
    }
    return zones;
}

function register(RED, TestClient) {
    function FP2Node(config) {
        RED.nodes.createNode(this, config);
        const node = this;
        const lastValues = new Map();
        let zones = new Map();
        let client;
        let stopped = false;
        let reconnectTimer;
        let delay = 1000;
        let attempt = 0;
        const prefix = (config.topicPrefix || "fp2").replace(/^\/+|\/+$/g, "") || "fp2";

        function consume(data, initial = false) {
            for (const characteristic of data.characteristics || []) {
                const key = `${characteristic.aid}.${characteristic.iid}`;
                const zone = zones.get(key);
                if (!zone || (characteristic.status !== undefined && Number(characteristic.status) !== 0)
                    || ![0, 1, false, true].includes(characteristic.value)) {
                    continue;
                }
                const value = Boolean(characteristic.value);
                const previous = lastValues.get(key);
                lastValues.set(key, value);
                if (initial || previous === value) {
                    continue;
                }
                node.send({
                    topic: `${prefix}/zone/${zone.zone}`,
                    payload: value,
                    zone: zone.name,
                    aid: zone.aid,
                    iid: zone.iid,
                    timestamp: new Date().toISOString(),
                });
            }
        }

        async function retry(current, token, error) {
            if (stopped || token !== attempt || reconnectTimer) {
                return;
            }
            attempt += 1;
            node.status({ fill: "red", shape: "ring", text: "deconnecte" });
            node.warn(error);
            if (client === current) {
                client = undefined;
            }
            reconnectTimer = setTimeout(() => {
                reconnectTimer = undefined;
                void connect();
            }, delay);
            delay = Math.min(delay * 2, 60000);
            if (current) {
                current.removeAllListeners();
                await current.close().catch(() => {});
            }
        }

        async function connect() {
            const token = ++attempt;
            let current;
            node.status({ fill: "yellow", shape: "ring", text: "connexion" });
            try {
                const filename = config.pairingFile || "data/pairing.json";
                const document = JSON.parse(await fs.readFile(
                    path.resolve(RED.settings.userDir, filename), "utf8"));
                const pairing = document[config.pairingAlias || "fp2"];
                const pairingData = convertPairing(pairing);
                const host = (config.host || "").trim() || pairing.AccessoryIP;
                const port = Number(config.port || pairing.AccessoryPort);
                if (!host || !Number.isInteger(port) || port < 1 || port > 65535) {
                    throw new Error("Adresse ou port HAP invalide.");
                }
                if (stopped || token !== attempt) return;
                const HttpClient = TestClient || require("hap-controller/lib/transport/ip/http-client").default;
                current = new HttpClient(pairing.AccessoryPairingID, host, port, pairingData, {
                    usePersistentConnections: true,
                    subscriptionsUseSameConnection: true,
                });
                client = current;
                const database = await current.getAccessories();
                if (stopped || token !== attempt) return;
                zones = buildZones(database);
                if (!zones.size) {
                    throw new Error("Aucune presence HAP avec notifications trouvee.");
                }
                const values = await current.getCharacteristics([...zones.keys()]);
                if (stopped || token !== attempt) return;
                consume(values, true);
                current.on("event", event => {
                    if (!stopped && token === attempt) consume(event);
                });
                current.on("event-disconnect", () => {
                    void retry(current, token, "Connexion aux evenements FP2 interrompue.");
                });
                const result = await current.subscribeCharacteristics([...zones.keys()]);
                if (stopped || token !== attempt) return;
                if (result?.characteristics?.some(characteristic => Number(characteristic.status || 0) !== 0)) {
                    throw new Error("Le FP2 a refuse un abonnement aux evenements.");
                }
                delay = 1000;
                node.status({ fill: "green", shape: "dot", text: `${zones.size} presences` });
            } catch (error) {
                const message = error.code ? `Erreur FP2 : ${error.code}` : error.message;
                await retry(current, token, message);
            }
        }

        node.on("close", (removed, done) => {
            stopped = true;
            attempt += 1;
            clearTimeout(reconnectTimer);
            const current = client;
            client = undefined;
            if (current) current.removeAllListeners();
            Promise.resolve().then(() => current?.close()).catch(() => {}).finally(done);
        });
        void connect();
    }
    RED.nodes.registerType("fp2", FP2Node);
}

module.exports = register;
module.exports.convertPairing = convertPairing;
module.exports.buildZones = buildZones;