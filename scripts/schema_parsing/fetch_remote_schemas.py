from time import sleep
import httplib2
import json

resp, content = httplib2.Http().request("https://www.wikidata.org/w/api.php?action=query&list=allpages&apnamespace=640&aplimit=max&format=json")
content = json.loads(content)["query"]["allpages"]
with open("entity_schema_labels.csv", "a") as es_labels:
    es_labels.write("schema_id" + ";" + "schema_label_en\n")
    for page in content:
        schema_id = page["title"].split(":E")[-1]
        while True:
            result, schema_raw = httplib2.Http().request(f"https://www.wikidata.org/w/api.php?action=parse&page={page["title"]}&prop=wikitext&format=json")
            if result["status"] != "200":
                print("HTTP Error " + result["status"] + ", retrying in 10 seconds.")
                sleep(10)
            else:
                labels = json.loads(json.loads(schema_raw)["parse"]["wikitext"]["*"])["labels"]
                schema_label_en = labels.get("en", "")
                es_labels.write(schema_id + ";" + schema_label_en + "\n")
                schema_shex = json.loads(json.loads(schema_raw)["parse"]["wikitext"]["*"])["schemaText"]
                
                with open("entity_schemas/" + page["title"].replace(":", "_") + ".shex", "w") as f:
                    f.write(schema_shex)
                break
