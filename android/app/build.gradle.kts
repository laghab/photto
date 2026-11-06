plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val appVersion: String = (project.findProperty("appVersion") as String?) ?: "1.2.0"
val parts = appVersion.split(".").map { it.toIntOrNull() ?: 0 }
val versionNumber = parts.getOrElse(0) { 0 } * 10000 + parts.getOrElse(1) { 0 } * 100 + parts.getOrElse(2) { 0 }

android {
    namespace = "io.github.laghab.photto"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.laghab.photto"
        minSdk = 30
        targetSdk = 35
        versionCode = versionNumber
        versionName = appVersion
    }

    // The web UI is shared with the desktop app.
    sourceSets["main"].assets.srcDirs("../../static")

    signingConfigs {
        create("release") {
            val ks = System.getenv("PHOTTO_KEYSTORE")
            if (ks != null && file(ks).exists()) {
                storeFile = file(ks)
                storeType = "pkcs12"
                storePassword = System.getenv("PHOTTO_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("PHOTTO_KEY_ALIAS") ?: "photto"
                keyPassword = System.getenv("PHOTTO_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = if (System.getenv("PHOTTO_KEYSTORE") != null) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("com.google.mlkit:text-recognition:16.0.1")
    testImplementation("junit:junit:4.13.2")
}
