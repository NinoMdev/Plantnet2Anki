# Plantnet2Anki



This project automates the creation of an Anki deck (.apkg) from your personal observations in PlantNet. Anki's built-in import option **"Update existing notes when first field matches"** allows you to incrementally update your deck with new observations without resetting its learning state.
Anki is a highly efficient tool for memorising things like botany, so I believe this project could be very useful for any botanist who wants to retain their field observations more effectively.
This is initially a personal project that I am sharing because I think it can be useful to others.

Enjoy, and don't hesitate to share any comments or suggestions. I am not a developer, so some features may be missing and bugs may occur.

[!NOTE] This project was coded with the assistance of [Claude AI](https://claude.ai). 


## Quick description

This project is built with Python and uses the packages `requests`, `beautifulsoup4`, and `genanki`. See `requirements.txt` for details.

It opens a graphical interface that allows you to:

- **Load** your observation data from a PlantNet-exported `.csv` file
- **Select** the species to include in your deck
- **Enrich** each species with photos. You can configure:
  - the number of photos per species
  - the organs to look for (flower, leaf, etc.)
  - the photo source: by default, the app searches GBIF first, then iNaturalist, then the PlantNet API
- **Embed** photos directly into the `.apkg` file. This is recommended, as it allows offline use and instant loading in each card.

> Common name can be find in English, French, Deutsch, or Espanol. If not find in French, Deutsch or Espanol, the name will be diplayed in English. 
> Be aware that deck generation can take some time and produce a large file if you select many photos per species or have a large number of observations.

### Card architecture

Each species is represented by a **single card**. The front always shows one photo, randomly sampled from the full photo pool each time the card appears. This avoids over-learning from a specific photo context and encourages recognising plants across different configurations.

### A note on automatic enrichment

Common names and photos are retrieved automatically. Results may occasionally be imperfect:
- Photos can sometimes be of poor quality or hard to identify
- Common names may differ from what you expect — for instance, *Crataegus azarolus* was assigned the name *Azérolier* (French) rather than the more generic *Aubépine*, even though the whole genus *Crataegus* is commonly called *Aubépine* in French

You should review your deck after generation and adjust names or remove unsatisfactory photos if needed.

## Installation

This program works on Linux, Windows, and macOS.

### Prerequisites

- Python >= 3.8 — make sure it is installed and working on your machine ([download here](https://www.python.org/downloads/))

### Windows

1. Download and extract the `PlantNet2Anki` folder
2. Inside the extracted folder, double-click `install.bat` and wait for the installation to complete
3. Launch the app by double-clicking `launch.bat`

### Linux / macOS

1. Open a terminal and navigate to the `PlantNet2Anki` folder
2. Run the following command to install:
```bash
   sudo ./install.sh
```
   `chmod +x` grants execution rights to the script (you may need `sudo` for this). `install.sh` then handles the installation automatically.
3. Launch the app with:
```bash
   chmod +x launch.sh && ./launch.sh
```

> [!NOTE]
> `chmod +x` only needs to be run once per script. After that, you can simply use `./launch.sh` to start the app.

## A-to-Z user guide

### Anki setup

I personally use Anki on my phone (Android, via AnkiDroid). I am not sure how the iOS version behaves, but the process should be similar.

Download Anki on your computer and make sure you are logged in to the same account as on your phone. This will allow your decks to sync automatically between devices.

### PlantNet setup

Download the PlantNet app and log in. Once you have observations, make sure to share them so they become accessible from the web version.

Then, on your computer, go to the PlantNet web interface and download your observations as a `.csv` file:
[https://identify.plantnet.org/fr/account/data/observations](https://identify.plantnet.org/fr/account/data/observations)

You will also need a **PlantNet developer account** to obtain an API key, which is required for PlantNet2Anki to work. This is free and straightforward:

1. Create an account at [https://my.plantnet.org/](https://my.plantnet.org/)
2. Go to your dashboard at [https://my.plantnet.org/dashboard](https://my.plantnet.org/dashboard) — your API key will be displayed there

> [!NOTE]
> The free PlantNet API is limited to **500 requests per day**. This means you can process up to 500 species per day with PlantNet2Anki. If you have more observations, split your `.csv` into batches, generate one deck per batch on separate days, and merge them directly in Anki.

### Generate a deck with PlantNet2Anki

Done with the setup? You are now ready to generate your deck.

1. Launch PlantNet2Anki (see [Installation](#installation))
2. In the interface that opens, import the PlantNet `.csv` file you downloaded earlier. 
*(Alternatively, you can click **Load test CSV** to try the application out with a sample file first.)*
3. Enter the name of your deck. If you already have an existing PlantNet2Anki deck and want to merge them, use the **exact same name** to avoid confusion
4. Paste your PlantNet API key
5. Configure the photo enrichment options:
   - **Organs**: select which organs to look for (flower, leaf, etc.)
   - **Photos per organ**: number of photos to retrieve per organ
   - **Max untagged photos**: maximum number of photos without an organ tag to include as a complement

   > Most species have a limited number of organ-tagged photos available. Flowers and leaves are usually well covered, but other organs often are not. The script targets `n × m` photos in total (n organs × m photos per organ), and fills remaining slots with untagged photos up to the specified limit. This balances variety across organs with a guaranteed minimum number of photos even when organ tags are scarce, while keeping deck size manageable.

6. Check **"Include own PlantNet photos"** if you want to include the photos from your personal observations
7. Check **"Embed images in .apkg"** to bundle all photos inside the deck file. This is recommended — it enables offline use and instant photo loading. Note that embedded decks can be significantly larger; if file size is a concern, leave this unchecked to use image URLs instead
8. Click **"Start generation"**. This may take a while depending on the number of species
9.  Once complete, click **"Download deck"** to save the `.apkg` file

Then, open Anki on your computer: **File → Import**, and select your `.apkg` file. Anki will automatically detect and merge this deck with any existing PlantNet2Anki deck.

Finally, sync AnkiDroid with your account from the app — your new cards will appear on your phone.

**Enjoy your learning!** 🌿

---

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
See the LICENSE file for details.



## Credits & Acknowledgements

This tool is built upon the incredible work of global open-science communities and open-source libraries:

### Core Frameworks

* [requests](https://github.com/psf/requests) - Elegant HTTP networking.
* [Beautiful Soup 4](https://www.crummy.com/software/BeautifulSoup/) - HTML parsing.
* [genanki](https://github.com/kerrickstaley/genanki) - Powerful programmatic Anki deck creation.

### Biodiversity Data Sources

* **PlantNet** - Species identification services and personal observation data.
* **GBIF** *(Global Biodiversity Information Facility)* - Global taxonomy, vernacular names, and image archives.
* **iNaturalist** - Phenomenal community-contributed biodiversity photography.
* **Tela Botanica** - French botanical reference networks, habitat data, and taxonomy.
* **Plants For A Future (PFAF)** - Deep ethnobotanical datasets regarding edibility and toxicity.

### Image Copyright Notice

*All photographs retrieved by this script remain the property of their respective creators and are bound by their original open licenses (such as Creative Commons). Users are responsible for ensuring compliance with intellectual property licenses when sharing or distributing generated decks publicly.*