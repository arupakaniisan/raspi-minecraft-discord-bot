#!/usr/bin/env bash
# Build McbotWhitelist.jar with javac only (needs a JDK; no Maven/Gradle).
# Compiles against the Paper libraries that the server already downloaded.
#   ./build.sh [minecraft-dir]     (default: ~/minecraft)
set -euo pipefail
cd "$(dirname "$0")"
MC="${1:-$HOME/minecraft}"
API=$(ls "$MC"/libraries/io/papermc/paper/paper-api/*/paper-api-*.jar | tail -n1)
CP="$API:$(find "$MC/libraries" -name '*.jar' | tr '\n' ':')"
rm -rf build && mkdir -p build/classes
javac --release 21 -Xlint:-classfile -cp "$CP" -d build/classes $(find src/main/java -name '*.java')
cp src/main/resources/* build/classes/
jar --create --file build/McbotWhitelist.jar -C build/classes .
echo "built: $(pwd)/build/McbotWhitelist.jar"
echo "install: cp build/McbotWhitelist.jar $MC/plugins/ && restart the server"
