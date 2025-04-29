#!/bin/bash
# ApplicationStart hook: update the index page to Hello World V2
echo "<h1>Hello World V2 from $(hostname -f)</h1>" > /var/www/html/index.html
