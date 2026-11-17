// SPDX-License-Identifier: (MIT OR GPL-2.0)
/* Hold Pi 5 RP1 GPCLK0 at the ADS1299-required 2.048 MHz.
 * The GPIO4 pinmux is selected separately; this module never touches Wi-Fi.
 */

#include <linux/clk.h>
#include <linux/errno.h>
#include <linux/init.h>
#include <linux/module.h>
#include <linux/of.h>
#include <dt-bindings/clock/rp1.h>

#define ADS_CLK_HZ 2048000UL
#define ADS_CLK_TOLERANCE_HZ 1000UL

static struct clk *ads_clk;

static int __init bcikit_gpclk_init(void)
{
	struct device_node *provider;
	struct of_phandle_args spec = { 0 };
	unsigned long actual;
	int ret;

	provider = of_find_compatible_node(NULL, NULL, "raspberrypi,rp1-clocks");
	if (!provider)
		return -ENODEV;
	spec.np = provider;
	spec.args_count = 1;
	spec.args[0] = RP1_CLK_GP0;
	ads_clk = of_clk_get_from_provider(&spec);
	of_node_put(provider);
	if (IS_ERR(ads_clk)) {
		ret = PTR_ERR(ads_clk);
		ads_clk = NULL;
		return ret;
	}

	ret = clk_set_rate(ads_clk, ADS_CLK_HZ);
	if (ret)
		goto put_clock;
	actual = clk_get_rate(ads_clk);
	if (actual < ADS_CLK_HZ - ADS_CLK_TOLERANCE_HZ ||
	    actual > ADS_CLK_HZ + ADS_CLK_TOLERANCE_HZ) {
		pr_err("bcikit_gpclk: RP1 GPCLK0 rounded to %lu Hz\n", actual);
		ret = -ERANGE;
		goto put_clock;
	}
	ret = clk_prepare_enable(ads_clk);
	if (ret)
		goto put_clock;
	pr_info("bcikit_gpclk: RP1 GPCLK0 enabled at %lu Hz\n", actual);
	return 0;

put_clock:
	clk_put(ads_clk);
	ads_clk = NULL;
	return ret;
}

static void __exit bcikit_gpclk_exit(void)
{
	clk_disable_unprepare(ads_clk);
	clk_put(ads_clk);
	pr_info("bcikit_gpclk: RP1 GPCLK0 disabled\n");
}

module_init(bcikit_gpclk_init);
module_exit(bcikit_gpclk_exit);

MODULE_DESCRIPTION("BCIKit Pi 5 ADS1299 2.048 MHz GPCLK0 holder");
MODULE_AUTHOR("BCIKit contributors");
MODULE_LICENSE("Dual MIT/GPL");
