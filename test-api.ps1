$body = @{
    start  = "New York, NY"
    finish = "Chicago, IL"
} | ConvertTo-Json

try {
    $response = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:8000/api/route/" `
        -ContentType "application/json" `
        -Body $body `
        -ErrorAction Stop
}
catch {
    $responseBody = $_.ErrorDetails.Message
    if ($responseBody) {
        try {
            $apiError = $responseBody | ConvertFrom-Json -ErrorAction Stop
            $apiError | ConvertTo-Json -Depth 10
            return
        }
        catch {
            Write-Error $responseBody
            return
        }
    }

    Write-Error $_
    return
}

$demoResponse = [ordered]@{
    start           = $response.start.address
    finish          = $response.finish.address
    distance_miles  = [math]::Round($response.route.distance_miles, 2)
    total_gallons   = [math]::Round($response.total_gallons, 2)
    total_fuel_cost = [math]::Round($response.total_fuel_cost, 2)
    selected_stops  = @(
        $response.selected_stops | ForEach-Object {
            [ordered]@{
                name              = $_.name
                city              = $_.city
                state             = $_.state
                price_per_gallon  = [math]::Round($_.price_per_gallon, 2)
            }
        }
    )
}

$demoResponse | ConvertTo-Json -Depth 4
