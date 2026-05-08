package com.meowcoin.catstack.data

import com.meowcoin.catstack.settings.ServerConfig
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

sealed interface StreamEvent {
    data class Update(val rigs: List<RigEntry>) : StreamEvent
    data class Status(val message: String, val isError: Boolean = false) : StreamEvent
}

class StatsStream(
    private val client: OkHttpClient,
    private val json: Json,
) {
    /**
     * Reconnecting WebSocket subscription. Listener drives lifecycle: each
     * connection completes a Deferred when it closes (either side) and the
     * outer loop reconnects after a short backoff. Cancellation of the
     * collecting coroutine tears down both the loop and the active socket.
     */
    fun subscribe(cfg: ServerConfig): Flow<StreamEvent> = callbackFlow {
        val scope = CoroutineScope(Dispatchers.IO)
        var current: WebSocket? = null

        val job = scope.launch {
            while (isActive) {
                trySend(StreamEvent.Status("Connecting…"))
                val wsUrl = cfg.baseUrl
                    .replaceFirst("http://", "ws://")
                    .replaceFirst("https://", "wss://")
                val req = Request.Builder()
                    .url("$wsUrl/ws?token=${cfg.token}")
                    .header("Authorization", "Bearer ${cfg.token}")
                    .build()

                val closed = CompletableDeferred<Unit>()
                val ws = client.newWebSocket(req, object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        trySend(StreamEvent.Status("Connected"))
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        try {
                            val obj = json.parseToJsonElement(text).jsonObject
                            if (obj["type"]?.jsonPrimitive?.content == "stats_update") {
                                val parsed = json.decodeFromString<WsStatsUpdate>(text)
                                trySend(StreamEvent.Update(parsed.rigs))
                            }
                        } catch (e: Exception) {
                            trySend(StreamEvent.Status("Parse error: ${e.message}", isError = true))
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(1000, null)
                    }

                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        trySend(StreamEvent.Status("Disconnected", isError = true))
                        closed.complete(Unit)
                    }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        val code = response?.code
                        val msg = when (code) {
                            401, 4401 -> "Unauthorized — check token"
                            else -> "Connection failed: ${t.message}"
                        }
                        trySend(StreamEvent.Status(msg, isError = true))
                        closed.complete(Unit)
                    }
                })
                current = ws

                closed.await()
                current = null
                delay(3_000)
            }
        }

        awaitClose {
            current?.cancel()
            job.cancel()
        }
    }
}
