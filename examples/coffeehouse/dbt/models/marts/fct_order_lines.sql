-- One row per order line, widened with order and customer attributes. The order total is
-- repeated on every line, a common shape for a reporting mart.
select
    i.order_id,
    i.line_number,
    o.order_date,
    o.customer_id,
    o.status as order_status,
    o.channel,
    c.is_test as is_test_customer,
    p.category,
    i.quantity,
    i.amount,
    sum(i.amount) over (partition by i.order_id) as order_total
from {{ source('shop', 'order_items') }} as i
join {{ source('shop', 'orders') }} as o on o.order_id = i.order_id
join {{ source('shop', 'customers') }} as c on c.customer_id = o.customer_id
join {{ source('shop', 'products') }} as p on p.product_id = i.product_id
