package com.meowcoin.catstack.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.meowcoin.catstack.data.FlightSheet
import com.meowcoin.catstack.data.MfarmClient
import com.meowcoin.catstack.data.OcProfile
import com.meowcoin.catstack.data.RigEntry
import com.meowcoin.catstack.data.StatsStream
import com.meowcoin.catstack.data.StreamEvent
import com.meowcoin.catstack.settings.ServerConfig
import com.meowcoin.catstack.settings.SettingsStore
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class UiState(
    val config: ServerConfig = ServerConfig("", ""),
    val rigs: List<RigEntry> = emptyList(),
    val flightSheets: List<FlightSheet> = emptyList(),
    val ocProfiles: List<OcProfile> = emptyList(),
    val statusMessage: String = "Idle",
    val isError: Boolean = false,
    /** Toast-style transient messages — UI clears after rendering. */
    val snackbar: String? = null,
)

class CatStackViewModel(app: Application) : AndroidViewModel(app) {
    private val settings = SettingsStore(app)
    private val client = MfarmClient()
    private val stream = StatsStream(client.client(), client.json())

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var streamJob: Job? = null

    init {
        viewModelScope.launch {
            settings.config.collectLatest { cfg ->
                _state.update { it.copy(config = cfg) }
                restartStream(cfg)
                if (cfg.isConfigured) refreshLists(cfg)
            }
        }
    }

    private fun restartStream(cfg: ServerConfig) {
        streamJob?.cancel()
        if (!cfg.isConfigured) {
            _state.update { it.copy(statusMessage = "Not configured", isError = true, rigs = emptyList()) }
            return
        }
        streamJob = viewModelScope.launch {
            stream.subscribe(cfg).collectLatest { ev ->
                when (ev) {
                    is StreamEvent.Update -> _state.update {
                        it.copy(rigs = ev.rigs, statusMessage = "Live", isError = false)
                    }
                    is StreamEvent.Status -> _state.update {
                        it.copy(statusMessage = ev.message, isError = ev.isError)
                    }
                }
            }
        }
    }

    fun saveSettings(host: String, token: String) {
        viewModelScope.launch { settings.save(host, token) }
    }

    fun refreshLists() {
        viewModelScope.launch {
            val cfg = _state.value.config
            if (cfg.isConfigured) refreshLists(cfg)
        }
    }

    private suspend fun refreshLists(cfg: ServerConfig) {
        runCatching { client.listFlightsheets(cfg) }
            .onSuccess { fs -> _state.update { it.copy(flightSheets = fs) } }
            .onFailure { e -> snack("Flight sheets: ${e.message}") }
        runCatching { client.listOcProfiles(cfg) }
            .onSuccess { oc -> _state.update { it.copy(ocProfiles = oc) } }
            .onFailure { e -> snack("OC profiles: ${e.message}") }
    }

    fun startMiner(rig: String) = action("Start miner on $rig") {
        client.startMiner(it, rig)
    }

    fun stopMiner(rig: String) = action("Stop miner on $rig") {
        client.stopMiner(it, rig)
    }

    fun restartMiner(rig: String) = action("Restart miner on $rig") {
        client.restartMiner(it, rig)
    }

    fun applyFlightSheet(rig: String, fs: String) = action("Applied '$fs' to $rig") {
        client.applyFlightSheet(it, fs, rig)
    }

    fun applyOcProfile(rig: String, oc: String) = action("Applied OC '$oc' to $rig") {
        client.applyOcProfile(it, oc, rig)
    }

    private fun action(label: String, op: suspend (ServerConfig) -> Any) {
        viewModelScope.launch {
            val cfg = settings.config.first()
            if (!cfg.isConfigured) { snack("Set host + token in Settings"); return@launch }
            runCatching { op(cfg) }
                .onSuccess { snack(label) }
                .onFailure { e -> snack("$label failed: ${e.message}") }
        }
    }

    fun snackShown() = _state.update { it.copy(snackbar = null) }

    private fun snack(msg: String) = _state.update { it.copy(snackbar = msg) }
}
