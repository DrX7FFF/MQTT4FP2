"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter, once } = require("node:events");
const path = require("node:path");
const fs = require("node:fs");
const vm = require("node:vm");
const register = require("../fp2");
const pairing = require("../../tests/fixtures/pairing.json")["fp2-test"];
const accessories = require("../../tests/fixtures/accessories.json");

test("real HAP client loads and converted seed signs with the original public key", async () => {
    const { default: HttpClient } = require("hap-controller/lib/transport/ip/http-client");
    const sodium = require("libsodium-wrappers");
    await sodium.ready;
    const keys = sodium.crypto_sign_seed_keypair(new Uint8Array(32).fill(7));
    const input = {
        ...pairing,
        iOSDeviceLTSK: Buffer.alloc(32, 7).toString("hex"),
        iOSDeviceLTPK: Buffer.from(keys.publicKey).toString("hex"),
    };
    const converted = register.convertPairing(input);
    const data = Buffer.from("offline-pair-verify-test");
    const signature = sodium.crypto_sign_detached(data, Buffer.from(converted.iOSDeviceLTSK, "hex"));
    assert.ok(sodium.crypto_sign_verify_detached(signature, data, keys.publicKey));
    const client = new HttpClient(input.AccessoryPairingID, "127.0.0.1", 12345, converted);
    assert.deepEqual(client.getLongTermData(), converted);
    assert.equal(typeof client.subscribeCharacteristics, "function");
    await client.close();
});

test("editor and package register the same input-only Node-RED node", () => {
    const metadata = require("../package.json");
    const html = fs.readFileSync(path.resolve(__dirname, "../fp2.html"), "utf8");
    const script = html.match(/<script type="text\/javascript">([\s\S]*?)<\/script>/);
    assert.ok(script);
    let definition;
    vm.runInNewContext(script[1], {
        RED: { nodes: { registerType(name, config) {
            assert.equal(name, "fp2");
            definition = config;
        } } },
    });
    assert.equal(metadata["node-red"].nodes.fp2, "fp2.js");
    assert.equal(definition.inputs, 0);
    assert.equal(definition.outputs, 1);
    assert.ok(html.includes('data-template-name="fp2"'));
    assert.ok(html.includes('data-help-name="fp2"'));
    for (const field of Object.keys(definition.defaults)) {
        assert.ok(html.includes(`id="node-input-${field}"`));
    }
    assert.equal(definition.defaults.port.validate(""), true);
    assert.equal(definition.defaults.port.validate("12345"), true);
    assert.equal(definition.defaults.port.validate("65536"), false);
});

test("converts aiohomekit identifiers and 32-byte seed without changing the source", () => {
    const original = JSON.stringify(pairing);
    const converted = register.convertPairing(pairing);
    assert.equal(Buffer.from(converted.AccessoryPairingID, "hex").toString(), pairing.AccessoryPairingID);
    assert.equal(Buffer.from(converted.iOSDevicePairingID, "hex").toString(), pairing.iOSPairingId);
    assert.equal(converted.iOSDeviceLTSK, pairing.iOSDeviceLTSK + pairing.iOSDeviceLTPK);
    assert.equal(JSON.stringify(pairing), original);
    assert.throws(() => register.convertPairing(undefined), /appairage/);
    assert.throws(() => register.convertPairing({ ...pairing, AccessoryLTPK: "invalid" }), /invalide/);
    const expanded = { ...pairing, iOSDeviceLTSK: converted.iOSDeviceLTSK };
    assert.equal(register.convertPairing(expanded).iOSDeviceLTSK, converted.iOSDeviceLTSK);
});

test("maps event-capable occupancy with stable IDs, names and duplicate slugs", () => {
    const database = { accessories: [{ aid: 1, services: [
        { characteristics: [{ type: "23", value: "Bureau" }, { type: "71", iid: 10, perms: ["ev"] }] },
        { characteristics: [{ type: "23", value: "Bureau" }, { type: "71", iid: 20, perms: ["ev"] }] },
        { characteristics: [{ type: "71", iid: 30, perms: ["ev"] }] },
        { characteristics: [{ type: "71", iid: 40, perms: ["pr"] }] },
        { characteristics: [{ type: "6B", iid: 50, perms: ["ev"] }] },
    ] }] };
    const zones = register.buildZones(database);
    assert.deepEqual([...zones.keys()], ["1.10", "1.20", "1.30"]);
    assert.deepEqual([...zones.values()].map(zone => zone.zone), ["bureau", "bureau-1-20", "zone-1-30"]);
});

test("Node-RED input seeds silently, emits changes, reconnects and closes", { timeout: 5000 }, async () => {
    const signals = new EventEmitter();
    let instance;
    let constructor;
    const database = Array.isArray(accessories) ? { accessories } : accessories;
    const zones = register.buildZones(database);
    assert.ok(zones.size > 0);
    const first = [...zones.values()][0];
    class FakeClient extends EventEmitter {
        constructor(...args) {
            super();
            instance = this;
            this.args = args;
            this.closed = false;
        }
        async getAccessories() { return database; }
        async getCharacteristics() {
            return { characteristics: [...zones.values()].map(zone => ({ ...zone, value: 0 })) };
        }
        async subscribeCharacteristics(keys) { this.keys = keys; return {}; }
        async close() { this.closed = true; }
    }
    const RED = {
        settings: { userDir: path.resolve(__dirname, "../..") },
        nodes: {
            registerType(name, implementation) { assert.equal(name, "fp2"); constructor = implementation; },
            createNode(node) {
                const events = new EventEmitter();
                node.on = events.on.bind(events);
                node.emit = events.emit.bind(events);
                node.messages = [];
                node.send = message => node.messages.push(message);
                node.warn = message => signals.emit("failure", message);
                node.status = status => {
                    if (status.fill === "green") signals.emit("ready");
                };
            },
        },
    };
    register(RED, FakeClient);
    const ready = once(signals, "ready");
    const node = new constructor({ pairingFile: "tests/fixtures/pairing.json", pairingAlias: "fp2-test" });
    try {
        await ready;
        assert.equal(node.messages.length, 0);
        assert.deepEqual(instance.keys, [...zones.keys()]);
        assert.equal(instance.args[1], "127.0.0.1");
        const event = value => ({ characteristics: [{ aid: first.aid, iid: first.iid, value }] });
        instance.emit("event", event(1));
        instance.emit("event", event(true));
        instance.emit("event", { characteristics: [{ ...first, status: -70402, value: 0 }] });
        instance.emit("event", event(0));
        assert.deepEqual(node.messages.map(message => message.payload), [true, false]);
        assert.equal(node.messages[0].topic, `fp2/zone/${first.zone}`);
        assert.equal(node.messages[0].zone, first.name);
        assert.ok(!Number.isNaN(Date.parse(node.messages[0].timestamp)));
        const previousClient = instance;
        const reconnected = once(signals, "ready");
        previousClient.emit("event-disconnect", previousClient.keys);
        await reconnected;
        assert.equal(previousClient.closed, true);
        assert.notEqual(instance, previousClient);
        assert.equal(node.messages.length, 2);
        instance.emit("event", event(1));
        assert.equal(node.messages.length, 3);
    } finally {
        await new Promise(resolve => node.emit("close", false, resolve));
    }
    assert.equal(instance.closed, true);
    instance.emit("event", { characteristics: [{ ...first, value: 1 }] });
    assert.equal(node.messages.length, 3);
});