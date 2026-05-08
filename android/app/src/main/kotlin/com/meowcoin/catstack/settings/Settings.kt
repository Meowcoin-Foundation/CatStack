package com.meowcoin.catstack.settings

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "catstack_settings")

data class ServerConfig(val baseUrl: String, val token: String) {
    val isConfigured: Boolean
        get() = baseUrl.isNotBlank() && token.isNotBlank()
}

object SettingsKeys {
    val BASE_URL = stringPreferencesKey("base_url")
    val TOKEN = stringPreferencesKey("token")
}

class SettingsStore(private val ctx: Context) {
    val config: Flow<ServerConfig> = ctx.dataStore.data.map { prefs ->
        ServerConfig(
            baseUrl = prefs[SettingsKeys.BASE_URL].orEmpty(),
            token = prefs[SettingsKeys.TOKEN].orEmpty(),
        )
    }

    suspend fun save(baseUrl: String, token: String) {
        ctx.dataStore.edit { p ->
            p[SettingsKeys.BASE_URL] = baseUrl.trim().trimEnd('/')
            p[SettingsKeys.TOKEN] = token.trim()
        }
    }
}
