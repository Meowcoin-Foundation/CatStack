package com.meowcoin.catstack.data

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull

/**
 * Mirrors the WebSocket /ws "stats_update" payload's "rigs" entries
 * (mfarm/web/app.py::_build_rigs_payload). Stats is left as a raw
 * JsonElement because its shape varies per rig (active miner, GPU list,
 * vastai flag, etc.) and we only need to dig out a couple of fields.
 */
@Serializable
data class RigEntry(
    val name: String,
    val host: String? = null,
    val mac: String? = null,
    val group: String? = null,
    val flight_sheet: String? = null,
    val oc_profile: String? = null,
    val agent_version: String? = null,
    val online: Boolean = false,
    val error: String? = null,
    val stats: JsonElement? = null,
) {
    val activeMiner: JsonObject?
        get() {
            val s = stats?.let { runCatching { it.jsonObject }.getOrNull() } ?: return null
            val gpu = s["miner"]?.let { runCatching { it.jsonObject }.getOrNull() }
            val cpu = s["cpu_miner"]?.let { runCatching { it.jsonObject }.getOrNull() }
            val gpuRunning = gpu?.get("running")?.jsonPrimitive?.contentOrNull == "true"
            val cpuRunning = cpu?.get("running")?.jsonPrimitive?.contentOrNull == "true"
            return when {
                gpuRunning -> gpu
                cpuRunning -> cpu
                else -> gpu ?: cpu
            }
        }

    val isMining: Boolean
        get() {
            val m = activeMiner ?: return false
            val running = m["running"]?.jsonPrimitive?.contentOrNull
            return running == "true" || running == "True" || running == "1"
        }

    val hashrate: Double
        get() = activeMiner?.get("hashrate")?.jsonPrimitive?.doubleOrNull ?: 0.0

    val algo: String?
        get() = activeMiner?.get("algo")?.jsonPrimitive?.contentOrNull
            ?: activeMiner?.get("name")?.jsonPrimitive?.contentOrNull

    val totalPowerWatts: Double
        get() {
            val s = stats?.let { runCatching { it.jsonObject }.getOrNull() } ?: return 0.0
            val gpus = s["gpus"]?.let { runCatching { it.jsonArray }.getOrNull() }
            val gpuW = gpus?.sumOf {
                it.jsonObject["power_draw"]?.jsonPrimitive?.doubleOrNull ?: 0.0
            } ?: 0.0
            val cpuW = s["cpu"]?.jsonObject?.get("power_draw")?.jsonPrimitive?.doubleOrNull ?: 0.0
            return gpuW + cpuW
        }

    val maxGpuTempC: Double?
        get() {
            val s = stats?.let { runCatching { it.jsonObject }.getOrNull() } ?: return null
            val gpus = s["gpus"]?.let { runCatching { it.jsonArray }.getOrNull() } ?: return null
            return gpus.maxOfOrNull {
                it.jsonObject["temp"]?.jsonPrimitive?.doubleOrNull ?: 0.0
            }
        }
}

@Serializable
data class FlightSheet(
    val name: String,
    val coin: String? = null,
    val algo: String? = null,
    val miner: String? = null,
    val pool_url: String? = null,
    val wallet: String? = null,
    val is_solo: Boolean = false,
)

@Serializable
data class OcProfile(
    val name: String,
    val core_offset: Int? = null,
    val mem_offset: Int? = null,
    val core_lock: Int? = null,
    val mem_lock: Int? = null,
    val power_limit: Int? = null,
    val fan_speed: Int? = null,
)

@Serializable
data class WsStatsUpdate(
    val type: String,
    val timestamp: Double = 0.0,
    val rigs: List<RigEntry> = emptyList(),
)

@Serializable
data class ActionResult(
    val status: String? = null,
    val via: String? = null,
)

@Serializable
data class BulkResult(
    val action: String? = null,
    val results: Map<String, JsonElement> = emptyMap(),
)

@Serializable
data class ApplyResult(
    val results: Map<String, String> = emptyMap(),
)
