```
docker-compose -f docker-compose.build.yml down
```
```
docker rmi grok2api-grok2api_self 2>$null
```
```
docker-compose -f docker-compose.build.yml build --no-cache
```
```
docker-compose -f docker-compose.build.yml up -d
```