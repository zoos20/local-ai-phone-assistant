import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "ru.gemma.assistant"
    // compileSdk 37 — решение пользователя 16.11 (запрет «не поднимать» СНЯТ) ради
    // rnnoise-kmp 1.0.2: его AAR-метаданные требуют minCompileSdk=37 (у ВСЕХ версий
    // 1.0.0–1.0.2; запаска speex-kmp — та же). targetSdk/minSdk/версии НЕ тронуты —
    // поведение на телефоне не меняется (см. AGENTS.md/ARCHITECTURE.md §5).
    compileSdk = 37
    // Вынужденный фикс №2 (подъём тулчейна): с 37 Google поставляет платформы
    // только минорной схемой (android-37.0/37.1/37.2, «плоского» platforms;android-37
    // в репо нет) — AGP 8.13.0 автоустанавливает пакет platforms;android-37.0,
    // но ищет таргет по hash 'android-37', которого нет. Явный compileSdkMinor=0
    // даёт hash 'android-37.0' == установленной платформе (механизм минорных SDK AGP 8.13).
    compileSdkMinor = 0

    defaultConfig {
        applicationId = "ru.gemma.assistant"
        minSdk = 26
        targetSdk = 34
        // v0.6 (versionCode 6): фича «RNNoise перед wake» (Denoise-стейдж перед
        // wake; З1–З7) + публикация через build-apk.ps1; БЕЗ bump апдейтер
        // считает «уже актуально» и не предложит установку
        versionCode = 6
        versionName = "0.6"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    buildFeatures {
        // BuildConfig.DEBUG — один из двух факторов гейта канала логов на ПК
        // (FR-B8, задача 7; ARCHITECTURE.md §5): канал стартует с сервисом всегда,
        // при DEBUG=false гейт закрыт; публикуемые APK — debug (build-apk.ps1),
        // так что реально открывает только переключатель «Логи на ПК» (по умолчанию выключен)
        buildConfig = true
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    packaging {
        jniLibs.useLegacyPackaging = true
    }
}

// KGP 2.3.21 (подъём с 2.0.21): DSL android.kotlinOptions{} выдаёт ошибку скрипта
// («Using 'jvmTarget: String' is an error. Please migrate to the compilerOptions DSL»,
// kotl.in/u1r8ln) — вынужденный минимальный миграционный фикс подъёма Kotlin.
// Эквивалент прежнего kotlinOptions { jvmTarget = "17" }: применяется ко всем
// компиляциям android-таргета (main/test), как и раньше.
kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    // Оффлайн wake word + распознавание (русскоязычная модель качается отдельно)
    implementation("com.alphacephei:vosk-android:0.3.75")
    // Шумоподавление (RNNoise) перед wake-словом (фича «RNNoise перед wake»,
    // реализация З3/З5; арх-док project-tasks/rnnoise-wake-arch.md)
    implementation("cn.enaium.rnnoise:rnnoise-kmp:1.0.2")
    implementation("androidx.core:core-ktx:1.13.1")
    // Тема приложения (Theme.Gemma на базе Material3) + виджеты
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    // WebSocket-стриминг аудио на сервер во время речи (фаза 6, п. 1)
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    testImplementation("junit:junit:4.13.2")
    // org.json в unit-тестах: android.jar замокан, нужна реальная реализация
    // для Updater.parseManifest (штатный workaround)
    testImplementation("org.json:json:20240303")
}
