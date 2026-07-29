package com.acme.zoo;

import com.acme.animals.Animal;
import com.acme.animals.Dog;
import com.acme.animals.Cat;

public class Zoo {
    public String announce(Animal animal) {
        // Called through the Animal supertype reference — a caller of
        // Animal.speak() should be visible as a caller of both Dog.speak()
        // and Cat.speak() via polymorphic-dispatch synthesis.
        return animal.speak();
    }

    public void announceAll() {
        announce(new Dog("Rex"));
        announce(new Cat("Whiskers"));
    }
}
