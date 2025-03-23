# .github/scripts/generate-index.sh
#!/bin/bash

OUTPUT_FILE="publish/index.html"

echo "<html><head><title>Docs Index</title></head><body>" > $OUTPUT_FILE
echo "<h1>Documentation Versions</h1>" >> $OUTPUT_FILE

if [ -d "publish/branch" ]; then
    echo "<h2>Branches</h2><ul>" >> $OUTPUT_FILE
    for dir in publish/branch/*/; do
        dir_name=$(basename "$dir")
        echo "<li><a href=\"/branch/$dir_name/\">$dir_name</a></li>" >> $OUTPUT_FILE
    done
    echo "</ul>" >> $OUTPUT_FILE
fi

if [ -d "publish/release" ]; then
    echo "<h2>Releases</h2><ul>" >> $OUTPUT_FILE
    for dir in publish/release/*/; do
        dir_name=$(basename "$dir")
        echo "<li><a href=\"/release/$dir_name/\">$dir_name</a></li>" >> $OUTPUT_FILE
    done
    echo "</ul>" >> $OUTPUT_FILE
fi

echo "</body></html>" >> $OUTPUT_FILE
