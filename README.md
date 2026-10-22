# Audio Archive Player v0.5.4

## Ustawienia

Aplikacja zapisuje ustawienia w pliku `config.json` obok `audio_player.py`. Plik jest tworzony automatycznie przy pierwszym zapisie ustawień.

Przykład:
```json
{
  "theme": "nord",
  "volume": 75,
  "last_directory": "D:\\Audio",
  "playback": {
    "mode": "fs",
    "directory": "D:\\Audio",
    "archive": "",
    "file": "D:\\Audio\\01 - Utwór.flac",
    "position": 128.4,
    "paused": true
  }
}
```

W nagłówku przeglądarki wyświetlany jest łączny czas plików audio w aktualnym katalogu lub archiwum. Przy każdym pliku audio jego czas trwania pojawia się w osobnej kolumnie po prawej stronie nazwy, w stylu Music On Console (moc). Czasy są odczytywane w tle z metadanych; format pokazuje minuty i sekundy, a przy dłuższych utworach również godziny. Podczas skanowania w kolumnie pojawia się `…`; jeśli plik nie zawiera odczytywalnej długości, wyświetli się `--:--`. Licznik podaje liczbę plików, dla których udało się odczytać czas, a ewentualne braki metadanych są sygnalizowane osobno.

Zapisywane ustawienia:
- `theme` — aktywny motyw graficzny Textual;
- `volume` — głośność mpv w zakresie 0–100;
- `last_directory` — ostatnio używany katalog, otwierany przy kolejnym uruchomieniu, jeśli nie podano ścieżki w wierszu poleceń;
- `playback` — ostatni widok i wybrany plik, stan pauzy oraz pozycja odtwarzania.

Przy zwykłym zatrzymaniu (`S`) program zapamiętuje wybrany utwór, ale po kolejnym uruchomieniu tylko go zaznacza — nie rozpoczyna odtwarzania. Jeśli przed wyjściem utwór był zapauzowany, program otwiera ten sam katalog lub archiwum i odtwarza go w stanie pauzy, od zapamiętanej pozycji. Pozycja pauzy jest zapisywana okresowo. Podanie ścieżki w wierszu poleceń wyłącza przywracanie poprzedniego pliku i widoku na czas tego uruchomienia; aplikacja startuje w podanym katalogu.

Naciśnij `T`, aby przełączać motywy. Ikony katalogów są monochromatyczne i przyjmują kolor akcentu aktywnego motywu. Zmiana motywu i głośności zapisuje się automatycznie. Jeśli podasz katalog przy uruchomieniu, ma on pierwszeństwo przed zapamiętanym katalogiem.

## Instalacja i uruchomienie

Instalacja zależności:
```powershell
python -m pip install -r requirements.txt
```

Uruchomienie:
```powershell
python .\audio_player.py
python .\audio_player.py .\Muzyka
python .\audio_player.py "D:\Audio\Moja kolekcja"
```

Ścieżka względna jest liczona od bieżącego katalogu PowerShella. `mpv.exe` musi być w PATH albo obok `audio_player.py`.

## Sterowanie

- `Enter` — otwórz katalog/archiwum lub odtwórz utwór
- `..` — katalog nadrzędny / wyjście z archiwum
- `Space` — pauza/wznowienie
- `S` — zatrzymanie
- `←` / `→` — przewijanie o 10 sekund
- `-` / `+` — zmiana głośności o 5 punktów
- `T` — zmiana motywu
- `Q` — wyjście
