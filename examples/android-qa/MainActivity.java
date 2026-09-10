package com.phoneharness.qa;

import android.app.Activity;
import android.os.Bundle;
import android.graphics.Color;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/** A deterministic app fixture, with no network, account or storage access. */
public final class MainActivity extends Activity {
    private int count = 0;
    private TextView counter;

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private TextView label(LinearLayout parent, String text, int size) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextSize(size);
        view.setTextColor(Color.rgb(25, 30, 34));
        view.setPadding(0, dp(12), 0, dp(12));
        parent.addView(view);
        return view;
    }

    private void button(LinearLayout parent, String text, View.OnClickListener action) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setContentDescription(text);
        button.setOnClickListener(action);
        parent.addView(button, new LinearLayout.LayoutParams(-1, dp(56)));
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        if (state != null) count = state.getInt("count", 0);
        ScrollView scroll = new ScrollView(this);
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        page.setPadding(dp(24), dp(24), dp(24), dp(24));
        page.setBackgroundColor(Color.rgb(246, 247, 248));
        scroll.addView(page);
        setContentView(scroll);
        label(page, "Phone Harness QA", 26);
        label(page, "Install, interact, and verify the result.", 16);
        counter = label(page, "Count: " + count, 24);
        button(page, "Increment", view -> counter.setText("Count: " + (++count)));
        button(page, "Reset", view -> { count = 0; counter.setText("Count: 0"); });
        label(page, "Name", 18);
        EditText name = new EditText(this);
        name.setSingleLine(true);
        name.setHint("Enter a name");
        name.setContentDescription("Name input");
        name.setId(1001);
        page.addView(name, new LinearLayout.LayoutParams(-1, dp(56)));
        TextView result = new TextView(this);
        result.setTextSize(18);
        result.setPadding(0, dp(16), 0, dp(16));
        result.setTextColor(Color.rgb(25, 30, 34));
        result.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        button(page, "Submit", view -> {
            String value = name.getText().toString().trim();
            result.setText(value.isEmpty() ? "Name is required" : "Hello, " + value + "!");
        });
        page.addView(result);
    }

    @Override public void onSaveInstanceState(Bundle state) {
        state.putInt("count", count);
        super.onSaveInstanceState(state);
    }
}
