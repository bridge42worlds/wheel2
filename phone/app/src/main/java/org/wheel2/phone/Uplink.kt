package org.wheel2.phone

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.SystemClock
import android.provider.Settings
import android.telephony.TelephonyManager
import android.util.Base64
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.URL
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.Locale
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * Связь с сервером установки. Телефон для сервера — статический агент (роль S):
 *   "acc" — отсчёты акселерометра (int16 x y z, base64, номер первого, частота, метка времени),
 *   "mag" — замеры поля (мкТл × 100, своя частота),
 *   "tel" — раз в секунду: состояние, фон (сеть 50 Гц, радио), батарея.
 * Пакеты — строками в POST /api/ingest. Адрес: сначала 127.0.0.1:8090 (USB: сервер сам делает
 * adb reverse), не отвечает — адрес из маяка сервера в Wi-Fi (UDP 5006, «W2SRV 5005»).
 * Не ушло — останется в кольце и уйдёт следующей попыткой; кольцо переполнилось — счётчик lost.
 */
class Uplink(private val ctx: Context, private val hub: SensorHub) {
    val mac: String = pseudoMac(ctx)
    @Volatile var url = USB_URL
    @Volatile var beaconUrl: String? = null
    @Volatile var ok = false
    @Volatile var lastOkMs = 0L
    @Volatile var sent = 0L                    // пакетов принято сервером
    @Volatile var fails = 0L
    @Volatile var lastErr = ""
    @Volatile var clockOffset = 0.0            // время сервера минус время телефона, с (из ответа на пакеты)
    @Volatile var accLost = 0L
    @Volatile var magLost = 0L
    @Volatile var em50: Double? = null         // мкТл: сеть 50 Гц в поле (с поправкой на усреднение телефона)
    @Volatile var em50n: Double? = null        // мкТл: то же на соседних частотах (46, 48, 52, 54 Гц) — уровень шума
    @Volatile var magAvg = 1                   // телефон сам усредняет поле по стольким замерам (у Galaxy A26 — 4)
    private var magAvgVotes = 0
    @Volatile var bac: Double? = null          // мкТл: колебания поля (СКО за 2 с, по трём осям)
    @Volatile var wifiRssi: Int? = null
    @Volatile var cellDbm: Int? = null
    @Volatile var noSim = false
    @Volatile var battPct: Int? = null
    @Volatile var battTemp: Double? = null
    @Volatile var plugged = false

    private var accSent = 0L
    private var magSent = 0L
    private var seq = 0L
    private var lastTel = 0L
    private var lastMag = 0L
    private val t0 = SystemClock.elapsedRealtime()
    @Volatile private var running = false
    // Потоки — по одному: stop() лишь просит остановиться, поток дорабатывает tick (до ~9 с на таймаутах);
    // если за это время снова start() (экран выключили и включили), прежний поток продолжает, нового не заводим —
    // иначе два потока делили бы accSent/seq и слали пакеты дважды
    private val thLock = Any()
    private var th: Thread? = null
    private var bth: Thread? = null

    fun start() {
        synchronized(thLock) {
            if (running) return
            running = true
            if (th == null) {
                accSent = hub.accHead
                magSent = hub.magHead
                th = Thread({ loop() }, "uplink").also { it.start() }
            }
            if (bth?.isAlive != true) bth = Thread({ beacon() }, "beacon").apply { isDaemon = true }.also { it.start() }
        }
    }

    fun stop() { running = false }

    private fun loop() {
        while (true) {
            if (!running) synchronized(thLock) { if (!running) { th = null; return } }
            val t = SystemClock.elapsedRealtime()
            try {
                tick(t)
            } catch (e: Exception) {
                lastErr = e.javaClass.simpleName + ": " + (e.message ?: "")
            }
            SystemClock.sleep(max(20L, 250L - (SystemClock.elapsedRealtime() - t)))
        }
    }

    private fun tick(now: Long) {
        val lines = ArrayList<String>()
        // акселерометр: всё накопленное, по 140 отсчётов в пакете (как у плат)
        var accTo = accSent
        synchronized(hub.lock) {
            val head = hub.accHead
            if (head - accSent > hub.accCap) {          // молчали дольше, чем держит кольцо
                accLost += head - hub.accCap - accSent
                accSent = head - hub.accCap
            }
            accTo = accSent
            val lastNs = hub.accLastNs
            while (accTo < head && lines.size < 24) {
                val n = min(140L, head - accTo).toInt()
                val bb = ByteBuffer.allocate(n * 6).order(ByteOrder.LITTLE_ENDIAN)
                for (q in 0 until n) {
                    val i = (((accTo + q) % hub.accCap) * 3).toInt()
                    for (j in 0..2) bb.putShort(hub.accRing[i + j])
                }
                lines += "{\"type\":\"acc\",\"mac\":\"$mac\",\"i0\":$accTo,\"n\":$n,\"hz\":${f3(hub.accRate.hz)},\"avg\":2,\"afs\":8," +
                    "\"lsb\":${hub.accLsb.roundToInt()},\"mi\":$head,\"mms\":${lastNs / 1_000_000},\"ms\":${SystemClock.elapsedRealtimeNanos() / 1_000_000}," +
                    "\"t\":${battTemp ?: "null"},\"lost\":$accLost,\"dev\":\"phone\"," +
                    "\"b64\":\"${Base64.encodeToString(bb.array(), Base64.NO_WRAP)}\"}"
                accTo += n
            }
        }
        // магнитометр: раз в 0,5 с; после обрыва связи — накопленное, до 6 пакетов за раз
        var magTo = magSent
        if (now - lastMag >= 500) {
            var k = 0
            while (k < 6) {
                val pk = magPacket(magTo) ?: break
                lines += pk.first
                magTo = pk.second
                k++
            }
        }
        // состояние: раз в секунду
        val telNow = now - lastTel >= 1000
        if (telNow) {
            background()
            lines += telPacket(now)
        }
        if (lines.isEmpty()) return
        if (post(lines)) {
            accSent = accTo
            if (magTo != magSent) { magSent = magTo; lastMag = now }
            if (telNow) { lastTel = now; seq++ }
            sent += lines.size
        }
    }

    /** Пакет поля с замера from: номер первого (i0) и время первого и последнего по часам датчика (mt0, mt1) —
     *  сервер ставит замеры на свои места и отбрасывает повторы. Кольцо переполнилось — счётчик magLost. */
    private fun magPacket(fromIn: Long): Pair<String, Long>? {
        var from = fromIn
        var mt0 = 0L
        var mt1 = 0L
        val vals: FloatArray = synchronized(hub.lock) {
            val head = hub.magHead
            if (head - from > hub.magCap) {             // молчали дольше, чем держит кольцо: пропавшее — считать раз
                if (from == magSent) { magLost += head - hub.magCap - from; magSent = head - hub.magCap }
                from = head - hub.magCap
            }
            val n = min(400L, head - from).toInt()
            if (n <= 0) return null
            mt0 = hub.magTs[(from % hub.magCap).toInt()] / 1_000_000
            mt1 = hub.magTs[((from + n - 1) % hub.magCap).toInt()] / 1_000_000
            FloatArray(n * 3).also { out ->
                for (q in 0 until n) {
                    val i = (((from + q) % hub.magCap) * 3).toInt()
                    for (j in 0..2) out[q * 3 + j] = hub.magRing[i + j]
                }
            }
        }
        val n = vals.size / 3
        val sum = DoubleArray(3)
        var babs = 0.0
        var bmin = Double.MAX_VALUE
        var bmax = 0.0
        val sb = StringBuilder()
        for (q in 0 until n) {
            val x = vals[q * 3]; val y = vals[q * 3 + 1]; val z = vals[q * 3 + 2]
            sum[0] += x; sum[1] += y; sum[2] += z
            val a = sqrt((x * x + y * y + z * z).toDouble())
            babs += a; bmin = min(bmin, a); bmax = max(bmax, a)
            if (q > 0) sb.append(',')
            sb.append('[').append((x * 100).roundToInt()).append(',').append((y * 100).roundToInt()).append(',')
                .append((z * 100).roundToInt()).append(']')
        }
        val res = hub.mag?.resolution ?: 0f
        val line = "{\"type\":\"mag\",\"mac\":\"$mac\",\"n\":$n,\"hz\":${f3(hub.magRate.hz.takeIf { it > 1 } ?: 50.0)},\"div\":100," +
            "\"step\":${f3(res.toDouble())},\"gain\":0,\"err\":0,\"b\":[${f3(sum[0] / n)},${f3(sum[1] / n)},${f3(sum[2] / n)}]," +
            "\"babs\":${f3(babs / n)},\"bmin\":${f3(bmin)},\"bmax\":${f3(bmax)},\"t\":${battTemp ?: "null"},\"dev\":\"phone\"," +
            "\"i0\":$from,\"mt0\":$mt0,\"mt1\":$mt1,\"ms\":${SystemClock.elapsedRealtimeNanos() / 1_000_000},\"lost\":$magLost,\"s\":[$sb]}"
        return line to from + n
    }

    private fun telPacket(now: Long): String {
        val up = (now - t0) / 1000
        val ahz = hub.accRate.hz
        val mhz = hub.magRate.hz
        val sens = (if (hub.acc != null) 1 else 0) or (if (hub.mag != null) 2 else 0)
        val wait = hub.accHead - accSent
        val ph = "{\"model\":\"${esc(Build.MODEL)}\",\"acc\":\"${esc(hub.acc?.name ?: "")}\",\"mag\":\"${esc(hub.mag?.name ?: "")}\"," +
            "\"ahz\":${f3(ahz)},\"mhz\":${f3(mhz)},\"em50\":${em50?.let { f4(it) } ?: "null"},\"em50n\":${em50n?.let { f4(it) } ?: "null"},\"bac\":${bac?.let { f4(it) } ?: "null"}," +
            "\"cell\":${cellDbm ?: "null"},\"batt\":${battPct ?: "null"},\"plug\":${if (plugged) 1 else 0}," +
            "\"btemp\":${battTemp ?: "null"},\"clip\":${hub.accClip},\"ma\":$magAvg,\"mlost\":$magLost,\"bg\":${if (SensorService.running) 1 else 0}}"
        val acc = "{\"ok\":${if (hub.acc != null) 1 else 0},\"odr\":${ahz.roundToInt()},\"avg\":2,\"afs\":8,\"lsb\":${hub.accLsb.roundToInt()}," +
            "\"hz\":${f3(ahz)},\"raw\":${hub.accHead},\"out\":${hub.accHead},\"bad\":0,\"rderr\":0,\"cap\":${hub.accCap}," +
            "\"wait\":$wait,\"lost\":$accLost,\"t\":${battTemp ?: "null"}}"
        return "{\"type\":\"tel\",\"mac\":\"$mac\",\"fw\":\"phone-${BuildInfo.VERSION}\",\"sk\":\"wheel2_phone\",\"seq\":$seq," +
            "\"up\":$up,\"role\":2,\"dev\":\"phone\",\"sens\":$sens,\"afs\":8,\"avg\":2,\"run\":1," +
            "\"temp\":${battTemp ?: "null"},\"rssi\":${wifiRssi ?: "null"},\"acc\":$acc,\"ph\":$ph}"
    }

    /** Фон и батарея: раз в секунду. */
    private fun background() {
        val m = hub.magTail(max(64, (hub.magRate.hz * 2).roundToInt()))
        if (m[0].size >= 32) {
            val fs = hub.magRate.hz
            // скользящее среднее по 4 видно по разностям соседних замеров: на сдвиге 4 корреляция −0,5, на 1–3 — 0
            val c = lagCorr(m[0], 1) to lagCorr(m[0], 4)
            magAvgVotes = (magAvgVotes + if (c.second < -0.3 && kotlin.math.abs(c.first) < 0.2) 1 else -1).coerceIn(-5, 5)
            if (magAvgVotes >= 3) magAvg = 4 else if (magAvgVotes <= -3) magAvg = 1
            val gain = maGain(magAvg, 50.0, fs)                 // во сколько раз усреднение ослабило 50 Гц
            val tones = (0..2).map { toneAmp(m[it], fs, 50.0) }
            em50 = if (tones.all { it != null } && gain > 0.05) sqrt(tones.sumOf { it!! * it }) / gain else null
            val near = listOf(46.0, 48.0, 52.0, 54.0).mapNotNull { fq ->
                val g = maGain(magAvg, fq, fs)
                val t = (0..2).map { toneAmp(m[it], fs, fq) }
                if (t.all { it != null } && g > 0.05) sqrt(t.sumOf { it!! * it }) / g else null
            }
            em50n = if (near.isNotEmpty()) near.average() else null
            bac = sqrt((0..2).sumOf { val sd = meanSd(m[it]).second; sd * sd })
        }
        try {
            val wm = ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION")
            val r = wm.connectionInfo?.rssi
            wifiRssi = if (r == null || r <= -127) null else r
        } catch (_: Exception) { wifiRssi = null }
        try {
            val tm = ctx.getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
            noSim = tm.simState == TelephonyManager.SIM_STATE_ABSENT
            cellDbm = tm.signalStrength?.cellSignalStrengths?.firstOrNull()?.dbm?.takeIf { it in -160..-20 }
        } catch (_: Exception) { cellDbm = null }
        val b = ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        if (b != null) {
            val lvl = b.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
            val sc = b.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
            battPct = if (lvl >= 0) lvl * 100 / max(1, sc) else null
            val tt = b.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE)
            battTemp = if (tt != Int.MIN_VALUE) tt / 10.0 else null
            plugged = b.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0
        }
    }

    /** Отправить пакеты; не вышло по одному адресу — попробовать другой: свежий из маяка (сервер сменил адрес), USB. */
    private fun post(lines: List<String>): Boolean {
        val body = lines.joinToString("\n").toByteArray(Charsets.UTF_8)
        val tries = listOfNotNull(url, beaconUrl, USB_URL).distinct()
        for (u in tries) {
            try {
                val c = URL("$u/api/ingest").openConnection() as HttpURLConnection
                c.connectTimeout = 1500
                c.readTimeout = 3000
                c.requestMethod = "POST"
                c.doOutput = true
                c.setRequestProperty("Content-Type", "text/plain; charset=utf-8")
                c.setFixedLengthStreamingMode(body.size)
                c.outputStream.use { it.write(body) }
                val code = c.responseCode
                val resp = c.inputStream.use { String(it.readBytes(), Charsets.UTF_8) }
                c.disconnect()
                if (code == 200) {
                    try {
                        val ts = org.json.JSONObject(resp).optDouble("t", Double.NaN)
                        if (!ts.isNaN()) clockOffset = ts - System.currentTimeMillis() / 1000.0
                    } catch (_: Exception) {
                    }
                    url = u
                    ok = true
                    lastOkMs = SystemClock.elapsedRealtime()
                    return true
                }
                lastErr = "сервер ответил $code"
            } catch (e: Exception) {
                lastErr = (e.javaClass.simpleName + " " + (e.message ?: "")).take(80)
            }
        }
        ok = false
        fails++
        return false
    }

    /** Маяк сервера в Wi-Fi: «W2SRV 5005» на UDP 5006 — адрес отправителя и есть сервер. */
    private fun beacon() {
        val wm = ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val lockM = wm.createMulticastLock("wheel2-beacon").apply { setReferenceCounted(false) }
        try {
            lockM.acquire()
            DatagramSocket(null).use { s ->
                s.reuseAddress = true
                s.broadcast = true
                s.bind(InetSocketAddress(5006))
                s.soTimeout = 5000
                val buf = ByteArray(256)
                while (running) {
                    try {
                        val p = DatagramPacket(buf, buf.size)
                        s.receive(p)
                        val text = String(p.data, 0, p.length, Charsets.UTF_8)
                        if (text.startsWith("W2SRV")) beaconUrl = "http://${p.address.hostAddress}:8090"
                    } catch (_: java.net.SocketTimeoutException) {
                    }
                }
            }
        } catch (e: Exception) {
            lastErr = "маяк: " + (e.message ?: e.javaClass.simpleName)
        } finally {
            if (lockM.isHeld) lockM.release()
        }
    }

    companion object {
        /** Корреляция первых разностей ряда на сдвиге k. */
        fun lagCorr(v: FloatArray, k: Int): Double {
            if (v.size < k + 10) return 0.0
            val d = DoubleArray(v.size - 1) { (v[it + 1] - v[it]).toDouble() }
            val m = d.average()
            var num = 0.0
            var den = 0.0
            for (i in d.indices) {
                den += (d[i] - m) * (d[i] - m)
                if (i >= k) num += (d[i] - m) * (d[i - k] - m)
            }
            return if (den > 0) num / den else 0.0
        }

        /** Усиление скользящего среднего по n замерам на частоте f при частоте отсчётов fs. */
        fun maGain(n: Int, f: Double, fs: Double): Double {
            if (n <= 1) return 1.0
            val x = Math.PI * f / fs
            return kotlin.math.abs(kotlin.math.sin(n * x) / (n * kotlin.math.sin(x)))
        }

        const val USB_URL = "http://127.0.0.1:8090"

        /** Свой адрес для сервера вида MAC: 02 (локальный) + 5 байт от ANDROID_ID — постоянный для телефона. */
        fun pseudoMac(ctx: Context): String {
            val id = Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ANDROID_ID) ?: "phone"
            val h = MessageDigest.getInstance("SHA-256").digest(("wheel2:" + id).toByteArray())
            return "02:" + (0 until 5).joinToString(":") { String.format(Locale.ROOT, "%02x", h[it]) }
        }

        fun f3(v: Double) = String.format(Locale.ROOT, "%.3f", v)
        fun f4(v: Double) = String.format(Locale.ROOT, "%.4f", v)
        fun esc(s: String) = s.replace("\\", "\\\\").replace("\"", "\\\"")
    }
}

object BuildInfo { const val VERSION = "1.1" }      // как versionName в app/build.gradle.kts: поднимать вместе
