package org.wheel2.phone

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Handler
import android.os.HandlerThread
import android.os.Process
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Акселерометр и магнитометр телефона — только то, что нужно установке.
 *
 * Акселерометр — без калибровки (TYPE_ACCELEROMETER_UNCALIBRATED): система не подкручивает
 * смещение нуля на ходу, и на Galaxy A26 он вчетверо быстрее обычного (~500 Гц против ~125).
 * Отсчёты — в int16, 4096 на g: это и есть ступень датчика (0,0024 м/с², предел ±8 g), так что
 * ничего не теряется, а сервер принимает их как пакеты "acc" статического агента.
 *
 * Магнитометр — тоже без калибровки (система не вычитает «железо» телефона), мкТл.
 * Кольца копят отсчёты для отправки (Uplink) и для экрана; номер отсчёта растёт без сброса.
 */
class SensorHub(ctx: Context) : SensorEventListener {
    private val sm = ctx.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    val acc: Sensor? = sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER_UNCALIBRATED)
        ?: sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
    val mag: Sensor? = sm.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD_UNCALIBRATED)
        ?: sm.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD)
    private val thread = HandlerThread("sensors", Process.THREAD_PRIORITY_URGENT_DISPLAY).apply { start() }

    // ---- акселерометр: int16 x y z, 4096 на g ----
    val accLsb = 4096f
    val accCap = 1 shl 20                       // ~35 мин при 500 Гц: Гаусс «с начала шага» и перерыв связи
    val accRing = ShortArray(accCap * 3)
    @Volatile var accHead = 0L                   // отсчётов всего
    @Volatile var accLastNs = 0L                 // время последнего отсчёта (часы датчика, нс)
    val accRate = Rate()
    @Volatile var accClip = 0L                   // отсчёты у предела ±8 g

    // ---- магнитометр: мкТл ----
    val magCap = 1 shl 18                       // ~35 мин при 125 Гц
    val magRing = FloatArray(magCap * 3)
    @Volatile var magHead = 0L
    val magRate = Rate()
    // оценка системы: сколько в замере от магнитов самого телефона (у датчика без калибровки — values[3..5])
    val magBias = FloatArray(3)
    @Volatile var magHasBias = false

    val lock = Any()

    fun start() {
        val h = Handler(thread.looper)
        acc?.let { sm.registerListener(this, it, 0, 0, h) }      // 0 мкс — как можно чаще
        mag?.let { sm.registerListener(this, it, 0, 0, h) }
    }

    fun stop() = sm.unregisterListener(this)

    // Проба: первые PROBE событий каждого датчика как есть (float и время) — какая у него настоящая ступень
    // и частота (не та, что в паспорте Android). Итог — в журнал (adb logcat -s wheel2probe) и в probeText.
    private val PROBE = 4000
    private val pAcc = Array(3) { FloatArray(PROBE) }
    private val pMag = Array(3) { FloatArray(PROBE) }
    private val pAccT = LongArray(PROBE)
    private val pMagT = LongArray(PROBE)
    private var pAccN = 0
    private var pMagN = 0
    @Volatile var probeText = ""
    @Volatile var accStep = DoubleArray(0)       // м/с²: наименьшая разница между значениями по осям

    private fun probe(name: String, v: Array<FloatArray>, t: LongArray, unit: Double, u: String): String {
        val sb = StringBuilder(name)
        val dts = (1 until PROBE).map { (t[it] - t[it - 1]) / 1e6 }.sorted()
        sb.append(String.format(java.util.Locale.ROOT, ": интервал медиана %.3f мс (%.1f Гц), 5%% %.3f, 95%% %.3f;",
            dts[dts.size / 2], 1000 / dts[dts.size / 2], dts[dts.size / 20], dts[dts.size * 19 / 20]))
        var same = 0
        for (i in 1 until PROBE) if (v[0][i] == v[0][i - 1] && v[1][i] == v[1][i - 1] && v[2][i] == v[2][i - 1]) same++
        sb.append(" повторов подряд $same из $PROBE;")
        val steps = DoubleArray(3)
        for (j in 0..2) {
            val s = v[j].distinct().sorted()
            val d = (1 until s.size).map { (s[it] - s[it - 1]).toDouble() }.filter { it > 0 }
            val mn = d.minOrNull() ?: 0.0
            steps[j] = mn
            // разницы в долях наименьшей: целые — решётка ровная, дробные — значения пересчитаны
            val frac = d.take(40).joinToString(",") { String.format(java.util.Locale.ROOT, "%.2f", it / mn) }
            sb.append(String.format(java.util.Locale.ROOT, " %s: различных %d, ступень %.6f %s (%.4f в единицах %s), разницы/ступень [%s];",
                "xyz"[j], s.size, mn, u, mn / unit, u, frac))
        }
        if (name == "acc") accStep = steps
        return sb.toString()
    }

    override fun onSensorChanged(e: SensorEvent) {
        if (e.sensor == acc && pAccN < PROBE) {
            for (j in 0..2) pAcc[j][pAccN] = e.values[j]
            pAccT[pAccN++] = e.timestamp
            if (pAccN == PROBE) probeText += probe("acc", pAcc, pAccT, SensorManager.GRAVITY_EARTH / 4096.0, "м/с²").also {
                android.util.Log.i("wheel2probe", it)
            } + "\n"
        }
        if (e.sensor == mag && pMagN < PROBE) {
            for (j in 0..2) pMag[j][pMagN] = e.values[j]
            pMagT[pMagN++] = e.timestamp
            if (pMagN == PROBE) probeText += probe("mag", pMag, pMagT, 0.15, "мкТл").also {
                android.util.Log.i("wheel2probe", it)
            } + "\n"
        }
        when (e.sensor) {
            acc -> {
                val k = accLsb / SensorManager.GRAVITY_EARTH
                synchronized(lock) {
                    val i = ((accHead % accCap) * 3).toInt()
                    for (j in 0..2) {
                        val v = (e.values[j] * k).roundToInt()
                        if (v >= 32767 || v <= -32768) accClip++
                        accRing[i + j] = v.coerceIn(-32768, 32767).toShort()
                    }
                    accHead++
                    accLastNs = e.timestamp
                }
                accRate.tick(e.timestamp)
            }
            mag -> {
                synchronized(lock) {
                    val i = ((magHead % magCap) * 3).toInt()
                    for (j in 0..2) magRing[i + j] = e.values[j]
                    magHead++
                    if (e.values.size >= 6) {
                        for (j in 0..2) magBias[j] = e.values[3 + j]
                        magHasBias = true
                    }
                }
                magRate.tick(e.timestamp)
            }
        }
    }

    override fun onAccuracyChanged(s: Sensor?, a: Int) {}

    /** Последние n отсчётов акселерометра, мg: [ось][отсчёт]. */
    fun accTail(n: Int): Array<FloatArray> = synchronized(lock) {
        val m = min(n.toLong(), min(accHead, accCap.toLong())).toInt()
        val out = Array(3) { FloatArray(m) }
        val s = 1000f / accLsb
        for (q in 0 until m) {
            val i = (((accHead - m + q) % accCap) * 3).toInt()
            for (j in 0..2) out[j][q] = accRing[i + j] * s
        }
        out
    }

    /**
     * Один ряд для Гаусса: последние n отсчётов акселерометра (мg, mag = false) или поля (мкТл),
     * ось 0..2 или модуль (3). Без лишних копий — окно «с начала шага» бывает в сотни тысяч отсчётов.
     */
    fun series(mag: Boolean, axis: Int, n: Int): FloatArray = synchronized(lock) {
        val head = if (mag) magHead else accHead
        val cap = if (mag) magCap else accCap
        val m = min(n.toLong(), min(head, cap.toLong())).toInt()
        val out = FloatArray(m)
        val s = 1000f / accLsb
        for (q in 0 until m) {
            val i = (((head - m + q) % cap) * 3).toInt()
            val x: Float
            val y: Float
            val z: Float
            if (mag) { x = magRing[i]; y = magRing[i + 1]; z = magRing[i + 2] }
            else { x = accRing[i] * s; y = accRing[i + 1] * s; z = accRing[i + 2] * s }
            out[q] = when (axis) { 0 -> x; 1 -> y; 2 -> z; else -> sqrt(x * x + y * y + z * z) }
        }
        out
    }

    /** Последние n замеров поля, мкТл: [ось][замер]. */
    fun magTail(n: Int): Array<FloatArray> = synchronized(lock) {
        val m = min(n.toLong(), min(magHead, magCap.toLong())).toInt()
        val out = Array(3) { FloatArray(m) }
        for (q in 0 until m) {
            val i = (((magHead - m + q) % magCap) * 3).toInt()
            for (j in 0..2) out[j][q] = magRing[i + j]
        }
        out
    }
}

/** Частота по часам датчика: число событий за последние ~2 с. */
class Rate {
    private var t0 = 0L
    private var n = 0
    @Volatile var hz = 0.0

    fun tick(ns: Long) {
        if (t0 == 0L) { t0 = ns; n = 0; return }
        n++
        val dt = (ns - t0) / 1e9
        if (dt >= 2.0) {
            hz = n / dt
            t0 = ns; n = 0
        }
    }
}

/** Среднее и разброс (СКО) одного ряда. */
fun meanSd(v: FloatArray): Pair<Double, Double> {
    if (v.isEmpty()) return 0.0 to 0.0
    var s = 0.0
    for (x in v) s += x
    val m = s / v.size
    var q = 0.0
    for (x in v) q += (x - m) * (x - m)
    return m to sqrt(q / max(1, v.size - 1))
}

/** Форма распределения: среднее, СКО, асимметрия, эксцесс (0 у гаусса), крайние. */
class Shape(val n: Int, val mean: Double, val sd: Double, val skew: Double, val kurt: Double, val min: Double, val max: Double) {
    /** Словами — как на планшетке: гаусс / хвосты (толчки, выбросы) / ступенчатый или ровный шум. */
    fun word(): String = when {
        n < 30 -> "мало отсчётов"
        kurt > 1.0 -> "◐ хвосты — толчки, выбросы"
        kurt < -0.7 -> "◐ ровный или ступенчатый — не гаусс"
        kotlin.math.abs(skew) > 0.5 -> "◐ перекошен — дрейф или толчки в одну сторону"
        kotlin.math.abs(kurt) < 0.3 && kotlin.math.abs(skew) < 0.2 -> "✓ гаусс"
        else -> "◐ близко к гауссу"
    }
}

fun shape(v: FloatArray): Shape {
    val n = v.size
    if (n < 2) return Shape(n, v.firstOrNull()?.toDouble() ?: 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    var s = 0.0
    var mn = Double.MAX_VALUE
    var mx = -Double.MAX_VALUE
    for (x in v) { s += x; mn = min(mn, x.toDouble()); mx = max(mx, x.toDouble()) }
    val m = s / n
    var m2 = 0.0
    var m3 = 0.0
    var m4 = 0.0
    for (x in v) {
        val d = x - m
        val d2 = d * d
        m2 += d2; m3 += d2 * d; m4 += d2 * d2
    }
    m2 /= n; m3 /= n; m4 /= n
    val sd = sqrt(m2)
    val ok = m2 > 1e-18
    return Shape(n, m, sd, if (ok) m3 / (m2 * sd) else 0.0, if (ok) m4 / (m2 * m2) - 3 else 0.0, mn, mx)
}

/**
 * Амплитуда синусоиды частоты f (Гёрцель) после вычитания среднего: сколько в сигнале
 * сетевых 50 Гц. Нужна частота отсчётов больше 2f — иначе null.
 */
fun toneAmp(v: FloatArray, fs: Double, f: Double): Double? {
    if (fs < 2.1 * f || v.size < 32) return null
    val (m, _) = meanSd(v)
    var re = 0.0
    var im = 0.0
    val w = 2 * PI * f / fs
    for (i in v.indices) {
        val x = v[i] - m
        re += x * cos(w * i)
        im -= x * sin(w * i)
    }
    return 2 * sqrt(re * re + im * im) / v.size
}
