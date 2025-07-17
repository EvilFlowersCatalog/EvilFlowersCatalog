# .github/scripts/generate-index.sh
#!/bin/bash

OUTPUT_FILE="publish/index.html"

# Copy logo to publish directory
mkdir -p publish
cp docs/images/logo.png publish/logo.png

# Generate enhanced HTML index page
cat > $OUTPUT_FILE << 'EOF'
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EvilFlowers Catalog - API Documentation</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            line-height: 1.6;
            color: #333;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
        }

        .header {
            text-align: center;
            margin-bottom: 3rem;
            background: rgba(255, 255, 255, 0.95);
            padding: 2rem;
            border-radius: 15px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
        }

        .logo {
            width: 120px;
            height: 120px;
            margin: 0 auto 1rem;
            border-radius: 50%;
            object-fit: cover;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
        }

        .header h1 {
            font-size: 2.5rem;
            color: #2c3e50;
            margin-bottom: 0.5rem;
            font-weight: 700;
        }

        .header p {
            font-size: 1.2rem;
            color: #7f8c8d;
            margin-bottom: 1rem;
        }

        .badge {
            display: inline-block;
            background: #3498db;
            color: white;
            padding: 0.3rem 0.8rem;
            border-radius: 20px;
            font-size: 0.9rem;
            font-weight: 600;
        }

        .docs-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 2rem;
            margin-top: 2rem;
        }

        .docs-section {
            background: rgba(255, 255, 255, 0.95);
            padding: 2rem;
            border-radius: 15px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }

        .docs-section:hover {
            transform: translateY(-5px);
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.15);
        }

        .docs-section h2 {
            color: #2c3e50;
            margin-bottom: 1rem;
            font-size: 1.5rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .docs-section h2::before {
            content: "📖";
            font-size: 1.2rem;
        }

        .docs-section.releases h2::before {
            content: "🚀";
        }

        .docs-list {
            list-style: none;
        }

        .docs-list li {
            margin-bottom: 0.8rem;
        }

        .docs-list a {
            display: block;
            color: #3498db;
            text-decoration: none;
            padding: 0.8rem 1rem;
            border-radius: 8px;
            transition: all 0.3s ease;
            border: 2px solid transparent;
            position: relative;
            overflow: hidden;
        }

        .docs-list a:hover {
            background: linear-gradient(45deg, #3498db, #2980b9);
            color: white;
            transform: translateX(5px);
            box-shadow: 0 4px 15px rgba(52, 152, 219, 0.3);
        }

        .docs-list a::before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
            transition: left 0.5s;
        }

        .docs-list a:hover::before {
            left: 100%;
        }

        .empty-state {
            text-align: center;
            color: #7f8c8d;
            font-style: italic;
            padding: 2rem;
        }

        .footer {
            margin-top: 3rem;
            text-align: center;
            color: rgba(255, 255, 255, 0.8);
            font-size: 0.9rem;
        }

        .footer a {
            color: rgba(255, 255, 255, 0.9);
            text-decoration: none;
        }

        .footer a:hover {
            color: white;
        }

        @media (max-width: 768px) {
            .container {
                padding: 1rem;
            }

            .header h1 {
                font-size: 2rem;
            }

            .docs-grid {
                grid-template-columns: 1fr;
            }

            .logo {
                width: 80px;
                height: 80px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <img src="logo.png" alt="EvilFlowers Logo" class="logo">
            <h1>EvilFlowers Catalog</h1>
            <p>OPDS 1.2 Compatible Digital Catalog Server</p>
            <span class="badge">API Documentation</span>
        </div>

        <div class="docs-grid">
EOF

# Generate branches section
if [ -d "publish/branch" ]; then
    echo '            <div class="docs-section">' >> $OUTPUT_FILE
    echo '                <h2>Development Branches</h2>' >> $OUTPUT_FILE
    echo '                <ul class="docs-list">' >> $OUTPUT_FILE
    
    # Sort branches with develop first, then alphabetically
    for dir in publish/branch/*/; do
        if [ -d "$dir" ]; then
            dir_name=$(basename "$dir")
            echo "branch:$dir_name"
        fi
    done | sort -t: -k2 | sort -t: -k2 -s | while IFS=: read -r prefix dir_name; do
        if [ "$dir_name" = "develop" ]; then
            echo "                    <li><a href=\"/EvilFlowersCatalog/branch/$dir_name/\"><strong>$dir_name</strong> (main development)</a></li>" >> $OUTPUT_FILE
        else
            echo "                    <li><a href=\"/EvilFlowersCatalog/branch/$dir_name/\">$dir_name</a></li>" >> $OUTPUT_FILE
        fi
    done
    
    echo '                </ul>' >> $OUTPUT_FILE
    echo '            </div>' >> $OUTPUT_FILE
else
    echo '            <div class="docs-section">' >> $OUTPUT_FILE
    echo '                <h2>Development Branches</h2>' >> $OUTPUT_FILE
    echo '                <div class="empty-state">No development branches available</div>' >> $OUTPUT_FILE
    echo '            </div>' >> $OUTPUT_FILE
fi

# Generate releases section
if [ -d "publish/release" ]; then
    echo '            <div class="docs-section releases">' >> $OUTPUT_FILE
    echo '                <h2>Release Versions</h2>' >> $OUTPUT_FILE
    echo '                <ul class="docs-list">' >> $OUTPUT_FILE
    
    # Sort releases in reverse semantic version order
    for dir in publish/release/*/; do
        if [ -d "$dir" ]; then
            dir_name=$(basename "$dir")
            echo "$dir_name"
        fi
    done | sort -V -r | while read -r dir_name; do
        echo "                    <li><a href=\"/EvilFlowersCatalog/release/$dir_name/\">v$dir_name</a></li>" >> $OUTPUT_FILE
    done
    
    echo '                </ul>' >> $OUTPUT_FILE
    echo '            </div>' >> $OUTPUT_FILE
else
    echo '            <div class="docs-section releases">' >> $OUTPUT_FILE
    echo '                <h2>Release Versions</h2>' >> $OUTPUT_FILE
    echo '                <div class="empty-state">No releases available</div>' >> $OUTPUT_FILE
    echo '            </div>' >> $OUTPUT_FILE
fi

# Close the HTML
cat >> $OUTPUT_FILE << 'EOF'
        </div>

        <div class="footer">
            <p>Generated automatically from <a href="https://github.com/EvilFlowersCatalog/EvilFlowersCatalog">EvilFlowers Catalog</a></p>
            <p>For technical support, visit our <a href="https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues">GitHub Issues</a></p>
        </div>
    </div>
</body>
</html>
EOF
