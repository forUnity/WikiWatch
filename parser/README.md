# MP2025-WikiWatch

## Getting Started

### Server Access
To access the server:

1. Connect to the HPI network or via VPN
2. ssh <name>.<lastname>@<server_ip> (name and lastname from your HPI email). Will ask you for your HPI password.
   This will log you into the isfet server (`home/<name>.<lastname>`).

All files are downloaded in `san2/data/wikidata-history-dumps` (from `home/<name>.<lastname>` go 2 back).
There's a shared folder in `san2/data/wikiwatch` that you can use.

SAN: 20TB on 7 HDDs (7.2k RPM, RAID 6) on SAN2

### Database Access

To access the database with changes:

1. Connect to the HPI network or via VPN
2. Navigate to [http://172.16.64.23:5050/browser/]
3. Login credentials:
   - **Username**: Your email (`<name>.<lastname>@student.hpi.uni-potsdam.de`)
   - **Password**: Same as username

**Database name**: `wikidata_entity_changes`

## Change Extraction

### Prerequisites

Install required libraries listed in `requirements.txt`.

### Configuration

A `.env` template file is provided with the necessary variables to connect to the PostgreSQL database. This database stores the following tables:
- `revision`
- `value_changes`
- `reference_change`
- `qualifier_change`
- `value_change_metadata`

**Important**: Update the `.env` file with your database credentials. The default configuration connects to the server.

**Note**: Table schemas are automatically created when running the parsers.

## Repository Structure
```bash
├── data/           # Auxiliary datasets for populating the database
├── scripts/        # Core parsing classes 
│   ├── dump_parser.py              # Processes XML files, extracts pages
│   ├── page_parser.py              # Processes a page (all edit history for an entity)
│   ├── fetch_entity_types.py      # Queries Wikidata SPARQL for entity types (RUNNING)
│   └── fetch_wd_entity_labels.py  # Queries Wikidata SPARQL for entity labels (RUNNING)
├── download/       # Scripts for downloading XML files + list of download links
└── test/           # Test XML files, testing scripts, and example revision texts
```

## Running the Parser

### Basic Usage

Run the parser with the following command:

```bash
python3 -m main [options]
```

**Options:**

-f FILE: Path to .xml.bz2 file to process (for single file processing)

**Note:** Processing each file takes approximately 1 hour (using 4 processes in parallel).

### Parallelization
By default, main.py uses the following parallelization strategy:
- Creates *files_in_parallel* processes (from config.json) that call DumpParser (*dump_parser.py*)
- Each DumpParser creates *pages_in_parallel* processes (from config.json) to call PageParser (*page_parser.py*) which processes a page (all revisions for an entity)

The system must support at least *files_in_parallel* × *pages_in_parallel* cores.

### Output Files
The parser generates three output files:

- `processed_files.txt`: List of processed files (for tracking)
- `parser_output.log`: Logs from dump_parser and page_parser
- `parser_log_files.json`: Summary with number of entities, processing time, and file size

### Config file
The `config.json` file contains the following parameters:

 | Parameter | Description | Default | 
 | - | - | - | 
 | language | Language for labels/descriptionsen | (English) | 
 | files_in_parallel |  Number of XML files to process simultaneously | 4 | 
 | pages_in_parallel | Number of pages per XML file to process simultaneously | 4 | 
 | max_files | Maximum number of files to process | - (use 2125 for all files) | 
 | page_debug_log_directory | Directory where to store debug information and content of pages that raised an exception during parsing (directory must already exist) | If unset, no debug information is stored |

## Batch Processing
### Running Multiple Files Until Limit
Use the run_parser.sh script to process files in batches until reaching *TOTAL* (input param):
```bash
> bashchmod +x run_parser.sh
> ./run_parser.sh <TOTAL_PARAM> &
```

**Note:** This script runs main.py with the same configuration set in config.json until the TOTAL number of files is processed.

### Run in detach
To run the parser in the background.
- Create tmux session: `tmux new -s session_name`
- Run script: `./run_parser.sh &`
- Get out of session: `ctrl + b` and then press `d`
- To go inside the session again: `tmux attach -t session_name`

## Test
In test/ there are 2 test files (*test.xml* and *example.xml*) to run the parser.
To run the parser on the test files run 
```bash
python3 -m test -f <file_name>
``` 
where file_name is either *example.xml* or *test.xml*.

Morevoer, the file *example_revision_text.json* provides an example of a real revision text (contains claims, labels, descriptions, qualifiers, references).

## Downloading wikidata dump 
**Note:** All files have already been downloaded to the server and they can be found in `/san2/data/wikidata-history-dumps`

Inside download/ run:
```bash
chmod +x download_wikidumps.sh
nohup bash download_wikidumps.sh > download_output.log 2>&1 &
```

*xml_download_links.txt* contains the download links for the xml.bz2 files of the dump 20250601

To obtain new download links use the scrapper in *scripts/utils* (change link to wikidata service url and folder to save links in *scripts/const*)

## Downloading extra data

We used the Wikidata Toolkit to extract extra data, such as, entity labels, property labels, entity's types (instance of and subclass of statements) to make our dataset more human-readable.
**NOTE:** The XML dump doesn't provide property's nor entity's labels in changes (e.g. if a property value is an entity, we only get the Q-id but not the label).

This class processes a latest-all.json.bz2 dump from WD and creates the following files:
- *labels_aliases.csv*: stores the english label and the first english alias of each entity (Q-id).
- *p31.csv*: stores the class the entity belongs to. This is extracted from the statement <Q-id, P31, Q-ID>. The csv stores the entities Q-id and the class Q-id.
- *p279.csv*: stores the superclass of the entity. This is extracted from the statement <Q-id, P279, Q-ID>. The csv stores the entities Q-id and the superclass Q-id.
- *property_labels.csv*: stores the english label of each property.

**Process to obtain files:**
- Clone Wikidata Toolkit from [Wikidata Toolkit](https://github.com/Wikidata-Toolkit/Wikidata-Toolkit).
- Download a `latest-all.json.bz2` from WD and store it in `Wikidata-Toolkit/dumpfiles/wikidatawiki/json-YYYYMMdd` where YYYYMMdd is the date when the dump was downloaded (The toolkit expects this format). Note that the downloaded dump needs to be in .json format.
- See Wikidata Toolkit requirements for Java versions
- Add the file `ExtractExtraData.java` in `wdtk/` to the folder `Wikidata-Toolkit/wdtk-examples/src/main/java/org/wikidata/wdtk/examples`.
    Change the following variables to store the correct path:
    ```java
        String labelsFile = "/PATH_TO_FOLDER/wdtk-output/labels_aliases.csv";
        String p31File = "/PATH_TO_FOLDER/wdtk-output/p31.csv";
        String p279File = "/PATH_TO_FOLDER/wdtk-output/p279.csv";
        String propertyLabelsFile = "/PATH_TO_FOLDER/wdtk-output/property_labels.csv";

        String dumpUrl = "/PATH_TO_FOLDER/Wikidata-Toolkit/dumpfiles/wikidatawiki/json-20251018/wikidata-20251018-all.json.bz2";
    ```
- Change the line `public static final boolean OFFLINE_MODE = false;` in `Wikidata-Toolkit/wdtk-examples/src/main/java/org/wikidata/wdtk/ExampleHelpers.java` to `public static final boolean OFFLINE_MODE = true;`. This disables the download of a new dump. Don't do this if you want the toolkit to download the dump.
- Add the following lines to the `<dependencies>` in the pom.xml inside `Wikidata-Toolkit/wdtk-examples/` and the outer pom.xml (root of the project):
```java
    <dependency>
        <groupId>org.apache.commons</groupId>
        <artifactId>commons-compress</artifactId>
        <version>1.26.0</version>
    </dependency>
    <dependency>
        <groupId>commons-io</groupId>
        <artifactId>commons-io</artifactId>
        <version>2.15.1</version>
    </dependency>

    <dependency>
        <groupId>org.apache.poi</groupId>
        <artifactId>poi</artifactId>
        <version>5.2.2</version>
    </dependency>

    <dependency>
        <groupId>org.apache.poi</groupId>
        <artifactId>poi-ooxml</artifactId>
        <version>5.2.2</version>
    </dependency>
```
- Add this to the pom.xml in wdtk-examples:
```java
<build>
    <plugins>
        <plugin>
            <groupId>org.apache.maven.plugins</groupId>
            <artifactId>maven-shade-plugin</artifactId>
            <version>3.5.0</version>
            <executions>
                <execution>
                    <phase>package</phase>
                    <goals>
                        <goal>shade</goal>
                    </goals>
                    <configuration>
                        <createDependencyReducedPom>false</createDependencyReducedPom>
                        <minimizeJar>false</minimizeJar>
                        <artifactSet>
                            <includes>
                                <include>*:*</include>
                            </includes>
                        </artifactSet>
                        <transformers>
                            <transformer implementation="org.apache.maven.plugins.shade.resource.ManifestResourceTransformer">
                                <mainClass>org.wikidata.wdtk.examples.ExtractExtraData</mainClass>
                            </transformer>
                        </transformers>
                    </configuration>
                </execution>
            </executions>
        </plugin>
    </plugins>
</build>
```
- Set the value of the variable PATH_TO_EXTRA_DATA in the .env file to the desired path
- Run the extraction with Maven inside `wdtk-examples/`:
```bash
mvn clean
mvn package
nohup java -jar target/NAME_OF_JAR.jar > dump_extract_output.log 2>&1 &
```

Run `python3 ./scripts/load_external_data.py` to load the data into the DB.