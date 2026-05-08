package com.meowcoin.catstack.data

import com.meowcoin.catstack.settings.ServerConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * Thin wrapper over OkHttp that talks to the FastAPI dashboard. All
 * methods take a ServerConfig so reconfiguring the server doesn't
 * require rebuilding the client. Suspending — call from a coroutine.
 */
class MfarmClient {
    private val http = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(40, TimeUnit.SECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private val json = Json {
        ignoreUnknownKeys = true
        coerceInputValues = true
        explicitNulls = false
    }

    fun client(): OkHttpClient = http
    fun json(): Json = json

    private fun req(cfg: ServerConfig, path: String): Request.Builder =
        Request.Builder()
            .url("${cfg.baseUrl}$path")
            .header("Authorization", "Bearer ${cfg.token}")

    private suspend inline fun <reified T> get(cfg: ServerConfig, path: String): T = withContext(Dispatchers.IO) {
        http.newCall(req(cfg, path).get().build()).execute().use { resp ->
            if (!resp.isSuccessful) error("GET $path -> ${resp.code}")
            json.decodeFromString(resp.body!!.string())
        }
    }

    private suspend inline fun <reified T> post(cfg: ServerConfig, path: String, body: String = "{}"): T = withContext(Dispatchers.IO) {
        val rb = body.toRequestBody("application/json".toMediaType())
        http.newCall(req(cfg, path).post(rb).build()).execute().use { resp ->
            if (!resp.isSuccessful) error("POST $path -> ${resp.code}")
            val text = resp.body?.string().orEmpty()
            if (text.isBlank()) json.decodeFromString("{}") else json.decodeFromString(text)
        }
    }

    suspend fun listRigs(cfg: ServerConfig): List<RigEntry> = get(cfg, "/api/rigs")
    suspend fun listFlightsheets(cfg: ServerConfig): List<FlightSheet> = get(cfg, "/api/flightsheets")
    suspend fun listOcProfiles(cfg: ServerConfig): List<OcProfile> = get(cfg, "/api/oc-profiles")

    suspend fun stopMiner(cfg: ServerConfig, rig: String): ActionResult =
        post(cfg, "/api/rigs/$rig/stop-miner")

    suspend fun restartMiner(cfg: ServerConfig, rig: String): ActionResult =
        post(cfg, "/api/rigs/$rig/restart-miner")

    /**
     * No top-level start-miner route; the dashboard expresses it via
     * the bulk endpoint (mfarm/web/api.py:2032).
     */
    suspend fun startMiner(cfg: ServerConfig, rig: String): BulkResult =
        post(cfg, "/api/bulk/start-miner/$rig")

    suspend fun applyFlightSheet(cfg: ServerConfig, fsName: String, rig: String): ApplyResult =
        post(cfg, "/api/flightsheets/$fsName/apply/$rig")

    suspend fun applyOcProfile(cfg: ServerConfig, ocName: String, rig: String): ApplyResult =
        post(cfg, "/api/oc-profiles/$ocName/apply/$rig")
}
