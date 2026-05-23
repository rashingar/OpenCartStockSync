SET NOCOUNT ON;

SELECT model, final
FROM (
    SELECT
        0 AS sort_order,
        'model' AS model,
        'quantity' AS final

    UNION ALL

    SELECT
        1 AS sort_order,
        ItemCode AS model,
        CONVERT(varchar(20), CAST(CASE WHEN Available < 0 THEN 0 ELSE Available END AS int)) AS final
    FROM dbo.ElecSEDItemBalance
    WHERE LEN(ItemCode) = 6
      AND ItemCode NOT LIKE '%[^0-9]%'
      AND WareHouse = '1'
) x
ORDER BY sort_order, model;