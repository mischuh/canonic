-- Distinct buying customers per day, pre-aggregated for a dashboard tile.
select
    o.order_date as activity_date,
    count(distinct o.customer_id) as active_customers
from {{ source('shop', 'orders') }} as o
join {{ source('shop', 'customers') }} as c on c.customer_id = o.customer_id
where o.status = 'completed'
  and not c.is_test
group by 1
