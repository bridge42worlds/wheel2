package org.wheel2.phone

import android.os.SystemClock
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

/**
 * Что сейчас делает установка: раз в 2 с — /api/programs с сервера (идущая программа, шаг, отдых).
 * Смена шага (или переход шаг ↔ отдых) запоминает, с какого отсчёта датчиков он начался —
 * от него считается Гаусс «с начала шага».
 */
class Experiment(private val up: Uplink, private val hub: SensorHub) {
    @Volatile var line = "○ программа не идёт"
    @Volatile var running = false
    @Volatile var stepKey = ""
    @Volatile var stepAcc = 0L                 // номер отсчёта акселерометра в начале шага (или с запуска приложения)
    @Volatile var stepMag = 0L
    @Volatile var stepWhat = "с запуска приложения"
    @Volatile private var go = false
    private val thLock = Any()
    private var th: Thread? = null              // поток один: после быстрого stop/start прежний продолжает (как в Uplink)

    fun start() {
        synchronized(thLock) {
            if (go) return
            go = true
            if (th == null) {
                stepAcc = hub.accHead
                stepMag = hub.magHead
                th = Thread({ loop() }, "experiment").apply { isDaemon = true }.also { it.start() }
            }
        }
    }

    fun stop() { go = false }

    private fun loop() {
        while (true) {
            if (!go) synchronized(thLock) { if (!go) { th = null; return } }
            try {
                poll()
            } catch (_: Exception) {
            }
            SystemClock.sleep(2000)
        }
    }

    private fun poll() {
        val c = URL(up.url + "/api/programs").openConnection() as HttpURLConnection
        c.connectTimeout = 1500
        c.readTimeout = 4000
        val text = c.inputStream.use { String(it.readBytes(), Charsets.UTF_8) }
        c.disconnect()
        val a = JSONObject(text).optJSONObject("active")
        val now = a?.optJSONObject("now")
        val state = a?.optString("state") ?: ""
        if (a == null || state != "running" || now == null) {
            running = false
            line = if (a != null && state.isNotEmpty() && state != "running") "○ программа №${a.optInt("id")} — ${stateWord(state)}" else "○ программа не идёт"
            setStep("idle")
            return
        }
        running = true
        val n = now.optInt("n")
        val of = now.optInt("of")
        val rest = now.optBoolean("rest")
        val t0 = now.optDouble(if (rest) "t_rest" else "t0", Double.NaN)
        val since = if (t0.isNaN()) "" else " · " + mmss(System.currentTimeMillis() / 1000.0 + up.clockOffset - t0)
        val name = now.optString("name")
        line = when {
            now.optBoolean("baseline") -> "▶ №${a.optInt("id")}: эталон шума статических$since"
            rest -> "⏸ №${a.optInt("id")}: отдых перед шагом ${n + 1} из $of$since"
            else -> "▶ №${a.optInt("id")}: шаг $n из $of «$name»" + (if (now.optBoolean("control")) " (контроль)" else "") + since +
                (if (now.has("minutes")) " из " + mmss(now.optDouble("minutes") * 60) else "")
        }
        setStep("${a.optInt("id")}:$n:$rest:${now.optBoolean("baseline")}", if (rest) "с начала отдыха" else "с начала шага $n")
    }

    private fun setStep(key: String, what: String = "с запуска приложения") {
        if (key == stepKey) return
        val first = stepKey.isEmpty()
        stepKey = key
        if (!first || key != "idle") {                 // при запуске приложения — с первого отсчёта
            stepAcc = hub.accHead
            stepMag = hub.magHead
            stepWhat = if (first) "$what (с запуска приложения)" else what
        }
    }

    companion object {
        fun mmss(s: Double): String {
            val t = s.coerceAtLeast(0.0).toLong()
            return String.format(Locale.ROOT, "%d:%02d", t / 60, t % 60)
        }

        fun stateWord(s: String) = when (s) {
            "done" -> "выполнена"; "stopped" -> "остановлена"; "error" -> "ошибка"; else -> s
        }
    }
}
