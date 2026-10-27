plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "io.github.saharshbaiju.tandem"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.saharshbaiju.tandem"
        minSdk = 26
        targetSdk = 36
        versionCode = 6
        versionName = "0.5.0"
    }

    // Release signing. The keystore lives OUTSIDE the repo; point at it with
    // env vars or ~/.gradle/gradle.properties (see docs/RELEASING.md):
    //   TANDEM_KEYSTORE, TANDEM_KEYSTORE_PASSWORD, TANDEM_KEY_ALIAS, TANDEM_KEY_PASSWORD
    // (the ASEXT_* names from before the rename to Tandem still work).
    // Without them, release builds fall back to the debug key (local testing only).
    fun lookup(name: String): String? =
        System.getenv(name) ?: (project.findProperty(name) as String?)
    fun secret(name: String): String? =
        lookup(name) ?: lookup(name.replaceFirst("TANDEM_", "ASEXT_"))
    val releaseKeystore = secret("TANDEM_KEYSTORE")?.let { file(it) }?.takeIf { it.exists() }

    signingConfigs {
        if (releaseKeystore != null) {
            create("release") {
                storeFile = releaseKeystore
                storePassword = secret("TANDEM_KEYSTORE_PASSWORD")
                keyAlias = secret("TANDEM_KEY_ALIAS") ?: "amvd"
                keyPassword = secret("TANDEM_KEY_PASSWORD") ?: secret("TANDEM_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        // Debug builds get their own package id and name, so they install next
        // to the release app (different signing keys can't replace each other)
        // and a test build never removes the working one.
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
        release {
            isMinifyEnabled = false
            signingConfig = if (releaseKeystore != null) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    // Material 3 (Material You): dynamic color, cards, buttons, text fields.
    implementation("com.google.android.material:material:1.12.0")
    // Google code scanner: Play-services hosted QR scanner UI, no camera
    // permission needed in this app.
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}
