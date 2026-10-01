# JARVIS (yeni)

Kendi bilgisayarında çalışan, yazarak ya da konuşarak iş yaptırdığın asistan.
Mark-LV'den sadece temel fikirleri aldı; kod sıfırdan yazıldı.

## Ne yapabilir

- **Ekran görüntüsünden Excel'e:** "Beykoz Riva'daki ilanların ilan no, fiyat ve telefonunu Excel'e aktar" de.
  Her ilanın ekran görüntüsünü alır, istediğin alanları görüntüden okur ve Excel'e satır olarak ekler.
  Metni Ctrl+A ile kopyalamaz. Görüntüler Excel'in yanındaki `..._ekran_goruntuleri` klasörüne kaydedilir,
  aynı ilan iki kez eklenmez.
- **Web'den veri çekmek:** "sahibinden'de Kadıköy'deki satılık arsaları Excel'e çek" gibi.
  Kendi Opera penceresini açar, sayfadaki tıklanabilir her şeyi numaralar ve numaraya tıklar.
  Bu yüzden "soldaki Arsa'ya tıkla" gibi istekleri gerçekten yapabilir.
- **YouTube:** "Duman'ın Bu Akşam şarkısını aç" der demez bulur ve çalar. Durdurur, reklam geçer, tam ekran yapar.
- **Excel:** tablo oluşturur, satır ekler, hücre/formül yazar, grafik ekler, var olan dosyayı okur, Excel'de açar.
- **Dosyalar:** listeler, bulur, okur (txt, Word, PDF), kaydeder, taşır, Geri Dönüşüm Kutusuna atar.
- **Bilgisayar:** uygulama açar (Excel, Spotify, WhatsApp…), ekrana bakar, tıklar, yazar, kısayol basar,
  PowerShell komutu çalıştırır, pil/disk/RAM durumunu söyler.
- **Hava durumu, web araması, hafıza** ("şunu hatırla").

Riskli işler (dosya silmek, komut çalıştırmak, başka klasördeki dosyanın üzerine yazmak) için önce senden onay ister.

## Kurulum (bir kere)

MARK-LV klasöründe terminal açıp:

```
git pull
pip install -r jarvis/requirements.txt
```

## Çalıştırma

```
python -m jarvis
```

İlk açılışta Gemini API anahtarını sorar. Mark-LV'ye daha önce girdiğin anahtar varsa onu kendisi bulur.
Ücretsiz anahtar: https://aistudio.google.com/apikey

## Kullanım

- Yaz ve Enter'a bas, ya da 🎤'ya basıp konuş. Sustuğunda kendisi anlar.
- **Ctrl+Alt+J** her yerden dinlemeyi başlatır.
- **🔊** cevapları sesli okur. **Sohbet** açıkken her cevaptan sonra yine dinler; konuşmazsan kapanır.
- Çalışırken **Durdur**'a basarak işi kesebilirsin. Yaptığı her adım mesajının altında görünür.
- **＋** yeni sohbet, **📁** JARVIS'in dosyaları kaydettiği klasör (Belgeler\JARVIS).

## Tarayıcı

JARVIS varsayılan olarak kendi Opera penceresini kullanır (ayrı profil). İstekte "Edge'den", "Chrome'dan"
dersen o tarayıcının kendi JARVIS penceresini kullanır. Senin açık Opera'na dokunmaz, başka tarayıcı açmaz,
boş sekme bırakmaz. O pencerede bir siteye giriş yaparsan JARVIS bir dahaki sefere de girişli kalır.
Opera farklı bir yerde kuruluysa `%USERPROFILE%\.jarvis\config.json` dosyasına
`{"browser_path": "C:\\...\\opera.exe"}` yaz.

## Ayarlar

`%USERPROFILE%\.jarvis\config.json`: `models` (Gemini modelleri, sırayla denenir), `workspace`,
`voice` (ör. `tr-TR-EmelNeural`), `language`, `max_steps`.
